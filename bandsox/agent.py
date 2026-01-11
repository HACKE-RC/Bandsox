#!/usr/bin/env python3
import sys
import json
import subprocess
import threading
import os
import select
import pty
import tty
import termios
import fcntl
import struct
import base64
import socket
import hashlib
import time

# This agent runs inside the guest on ttyS0.
# It reads JSON commands from stdin and writes JSON events to stdout.

# Vsock Client Module - Guest-Initiated Connections
#
# The guest initiates connections to the host for file transfers.
# Firecracker routes AF_VSOCK connections to Unix sockets on the host.
#
# Protocol:
# 1. Guest connects to host via AF_VSOCK(CID=2, port)
# 2. Guest sends a JSON request (download or upload)
# 3. Host responds with data or acknowledgments
# 4. Guest closes connection when done

VSOCK_CID_HOST = 2  # Well-known CID for host


def vsock_check_available() -> bool:
    """Check if vsock is available on this system."""
    try:
        socket.AF_VSOCK
        return True
    except AttributeError:
        return False


def vsock_create_connection(port: int, timeout: float = 10.0):
    """Create a new vsock connection to the host.

    Args:
        port: Port to connect to
        timeout: Connection timeout in seconds

    Returns:
        Connected socket or None on failure
    """
    if not vsock_check_available():
        sys.stderr.write("WARNING: AF_VSOCK not available in kernel\n")
        sys.stderr.flush()
        return None

    try:
        sock = socket.socket(socket.AF_VSOCK, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        sock.connect((VSOCK_CID_HOST, port))
        return sock
    except Exception as e:
        sys.stderr.write(f"WARNING: Vsock connection to port {port} failed: {e}\n")
        sys.stderr.flush()
        return None


def vsock_send_json_msg(sock, data: dict):
    """Send a JSON message over a vsock connection."""
    message = json.dumps(data) + "\n"
    sock.sendall(message.encode("utf-8"))


def vsock_recv_json_msg(sock) -> dict:
    """Receive a JSON message from vsock connection."""
    buffer = b""
    while b"\n" not in buffer:
        chunk = sock.recv(4096)
        if not chunk:
            raise Exception("Vsock connection closed")
        buffer += chunk

    line, _ = buffer.split(b"\n", 1)
    return json.loads(line.decode("utf-8"))


# Legacy globals for backward compatibility during transition
VSOCK_ENABLED = False
VSOCK_SOCKET = None


def vsock_connect(port: int, retry: int = 3) -> bool:
    """Legacy: Connects to host vsock socket.

    This maintains backward compatibility but uses the new connection method.
    """
    global VSOCK_ENABLED, VSOCK_SOCKET

    if not vsock_check_available():
        sys.stderr.write(
            "WARNING: Vsock module not available, falling back to serial\n"
        )
        sys.stderr.flush()
        return False

    for attempt in range(retry):
        try:
            VSOCK_SOCKET = vsock_create_connection(port)
            if VSOCK_SOCKET:
                sys.stderr.write(
                    f"INFO: Connected to vsock: CID={VSOCK_CID_HOST}, Port={port}\n"
                )
                sys.stderr.flush()
                VSOCK_ENABLED = True
                return True
            raise Exception("Connection returned None")
        except Exception as e:
            if attempt < retry - 1:
                sys.stderr.write(
                    f"DEBUG: Vsock connection attempt {attempt + 1} failed: {e}\n"
                )
                sys.stderr.flush()
                time.sleep(1)
            else:
                sys.stderr.write(
                    f"WARNING: Vsock connection failed after {retry} attempts: {e}\n"
                )
                sys.stderr.flush()
                VSOCK_ENABLED = False
                return False

    return False


def vsock_send_json(data: dict):
    """Legacy: Sends JSON data over vsock connection."""
    global VSOCK_SOCKET

    if not VSOCK_ENABLED or not VSOCK_SOCKET:
        raise Exception("Vsock not connected")

    message = json.dumps(data) + "\n"
    VSOCK_SOCKET.sendall(message.encode("utf-8"))


def vsock_read_line() -> str:
    """Legacy: Reads a newline-delimited line from vsock connection."""
    global VSOCK_SOCKET

    if not VSOCK_ENABLED or not VSOCK_SOCKET:
        raise Exception("Vsock not connected")

    buffer = b""
    while True:
        chunk = VSOCK_SOCKET.recv(1024)
        if not chunk:
            raise Exception("Vsock connection closed")
        buffer += chunk
        if b"\n" in buffer:
            line, _ = buffer.split(b"\n", 1)
            return line.decode("utf-8")


def vsock_disconnect():
    """Disconnects from vsock socket and cleans up."""
    global VSOCK_SOCKET, VSOCK_ENABLED

    if VSOCK_SOCKET:
        try:
            VSOCK_SOCKET.close()
        except Exception:
            pass
        VSOCK_SOCKET = None
        VSOCK_ENABLED = False
        sys.stderr.write("INFO: Vsock disconnected\n")
        sys.stderr.flush()


# Global session registry
sessions = {}  # session_id -> process
pty_masters = {}  # session_id -> master_fd


def send_event(event_type, payload):
    msg = json.dumps({"type": event_type, "payload": payload})
    sys.stdout.write(msg + "\n")
    sys.stdout.flush()


def read_stream(stream, stream_name, cmd_id):
    """Reads a stream line by line and sends events."""
    try:
        for line in stream:
            send_event(
                "output", {"cmd_id": cmd_id, "stream": stream_name, "data": line}
            )
    except ValueError:
        # Stream closed
        pass


def read_pty_master(master_fd, cmd_id):
    """Reads from PTY master and sends events."""
    try:
        while True:
            try:
                data = os.read(master_fd, 1024)
                if not data:
                    break

                encoded = base64.b64encode(data).decode("utf-8")
                send_event(
                    "output",
                    {
                        "cmd_id": cmd_id,
                        "stream": "stdout",  # PTY combines stdout/stderr usually
                        "data": encoded,
                        "encoding": "base64",
                    },
                )
            except OSError as e:
                # EIO means PTY closed
                if e.errno == 5:  # EIO
                    break
                # Other errors might be transient or fatal
                send_event(
                    "error", {"cmd_id": cmd_id, "error": f"PTY Read blocked: {e}"}
                )
                break
    except Exception as e:
        send_event("error", {"cmd_id": cmd_id, "error": str(e)})


def handle_command(cmd_id, command, background=False, env=None):
    try:
        # Prepare environment
        proc_env = os.environ.copy()
        if env:
            proc_env.update(env)

        process = subprocess.Popen(
            command,
            shell=True,
            env=proc_env,
            stdin=subprocess.PIPE,  # Enable stdin
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,  # Line buffered
        )

        if background:
            sessions[cmd_id] = process

            # Start threads to read stdout/stderr
            t_out = threading.Thread(
                target=read_stream, args=(process.stdout, "stdout", cmd_id), daemon=True
            )
            t_err = threading.Thread(
                target=read_stream, args=(process.stderr, "stderr", cmd_id), daemon=True
            )
            t_out.start()
            t_err.start()

            # Monitor exit in a separate thread
            def monitor_exit():
                rc = process.wait()
                if cmd_id in sessions:
                    del sessions[cmd_id]
                send_event("exit", {"cmd_id": cmd_id, "exit_code": rc})

            t_mon = threading.Thread(target=monitor_exit, daemon=True)
            t_mon.start()

            send_event(
                "status", {"cmd_id": cmd_id, "status": "started", "pid": process.pid}
            )

        else:
            # Blocking execution (legacy)
            t_out = threading.Thread(
                target=read_stream, args=(process.stdout, "stdout", cmd_id)
            )
            t_err = threading.Thread(
                target=read_stream, args=(process.stderr, "stderr", cmd_id)
            )
            t_out.start()
            t_err.start()

            t_out.join()
            t_err.join()

            rc = process.wait()
            send_event("exit", {"cmd_id": cmd_id, "exit_code": rc})

    except Exception as e:
        send_event("error", {"cmd_id": cmd_id, "error": str(e)})


def handle_pty_command(cmd_id, command, cols=80, rows=24, env=None):
    try:
        pid, master_fd = pty.fork()

        if pid == 0:
            # Child process
            # Set window size
            winsize = struct.pack("HHHH", rows, cols, 0, 0)
            fcntl.ioctl(0, termios.TIOCSWINSZ, winsize)

            # Prepare environment
            if env:
                os.environ.update(env)

            # Execute command
            # Use shell to execute command string
            args = ["/bin/sh", "-c", command]
            os.execvp(args[0], args)

        else:
            # Parent process
            pty_masters[cmd_id] = master_fd
            sessions[cmd_id] = pid  # Store PID for PTY sessions

            # Start thread to read from master_fd
            t_read = threading.Thread(
                target=read_pty_master, args=(master_fd, cmd_id), daemon=True
            )
            t_read.start()

            # Monitor exit
            def monitor_exit():
                _, status = os.waitpid(pid, 0)
                exit_code = os.waitstatus_to_exitcode(status)

                if cmd_id in sessions:
                    del sessions[cmd_id]
                if cmd_id in pty_masters:
                    os.close(pty_masters[cmd_id])
                    del pty_masters[cmd_id]

                send_event("exit", {"cmd_id": cmd_id, "exit_code": exit_code})

            t_mon = threading.Thread(target=monitor_exit, daemon=True)
            t_mon.start()

            send_event("status", {"cmd_id": cmd_id, "status": "started"})

    except Exception as e:
        send_event("error", {"cmd_id": cmd_id, "error": str(e)})


def handle_input(cmd_id, data, encoding=None):
    if cmd_id in sessions:
        if cmd_id in pty_masters:
            # PTY session
            master_fd = pty_masters[cmd_id]
            try:
                if encoding == "base64":
                    content = base64.b64decode(data)
                else:
                    content = data.encode("utf-8")
                os.write(master_fd, content)
            except Exception as e:
                send_event("error", {"cmd_id": cmd_id, "error": f"Write failed: {e}"})
        else:
            # Standard pipe session
            proc = sessions[cmd_id]
            if proc.stdin:
                try:
                    proc.stdin.write(data)
                    proc.stdin.flush()
                except Exception as e:
                    send_event(
                        "error", {"cmd_id": cmd_id, "error": f"Write failed: {e}"}
                    )
    else:
        send_event("error", {"cmd_id": cmd_id, "error": "Session not found"})


def handle_resize(cmd_id, cols, rows):
    if cmd_id in pty_masters:
        master_fd = pty_masters[cmd_id]
        try:
            winsize = struct.pack("HHHH", rows, cols, 0, 0)
            fcntl.ioctl(master_fd, termios.TIOCSWINSZ, winsize)
        except Exception as e:
            send_event("error", {"cmd_id": cmd_id, "error": f"Resize failed: {e}"})


def handle_kill(cmd_id):
    if cmd_id in sessions:
        if cmd_id in pty_masters:
            # PTY session - kill process group?
            pid = sessions[cmd_id]
            import signal

            try:
                os.kill(pid, signal.SIGTERM)
            except Exception as e:
                send_event("error", {"cmd_id": cmd_id, "error": f"Kill failed: {e}"})
        else:
            proc = sessions[cmd_id]
            try:
                proc.terminate()
            except Exception as e:
                send_event("error", {"cmd_id": cmd_id, "error": f"Kill failed: {e}"})
    else:
        send_event("error", {"cmd_id": cmd_id, "error": "Session not found"})


def handle_read_file(cmd_id, path):
    """Reads a file and sends content in chunks to avoid buffer overflows.

    Uses 2KB chunks (safe for serial buffer after base64 encoding).
    Sends file_chunk events for each chunk, then file_complete at end.
    """
    try:
        if not os.path.exists(path):
            send_event("error", {"cmd_id": cmd_id, "error": f"File not found: {path}"})
            send_event("exit", {"cmd_id": cmd_id, "exit_code": 1})
            return

        file_size = os.path.getsize(path)

        # For small files (<= 2KB), use single-shot transfer for efficiency
        CHUNK_SIZE = 2 * 1024  # 2KB chunks

        if file_size <= CHUNK_SIZE:
            # Small file - send all at once (backward compatible)
            with open(path, "rb") as f:
                content = f.read()
            encoded = base64.b64encode(content).decode("utf-8")
            send_event(
                "file_content", {"cmd_id": cmd_id, "path": path, "content": encoded}
            )
            send_event("exit", {"cmd_id": cmd_id, "exit_code": 0})
        else:
            # Large file - send in chunks with throttling for serial console
            md5 = hashlib.md5()
            offset = 0

            with open(path, "rb") as f:
                while True:
                    chunk = f.read(CHUNK_SIZE)
                    if not chunk:
                        break

                    md5.update(chunk)
                    encoded = base64.b64encode(chunk).decode("utf-8")

                    send_event(
                        "file_chunk",
                        {
                            "cmd_id": cmd_id,
                            "path": path,
                            "data": encoded,
                            "offset": offset,
                            "size": len(chunk),
                        },
                    )

                    offset += len(chunk)

                    # Throttle output to prevent serial buffer overflow
                    # Serial console is slow (~115200 baud = ~11KB/s max)
                    # 2KB chunk + base64 overhead = ~2.7KB, needs ~250ms to transmit
                    time.sleep(0.2)  # 200ms delay between chunks for serial safety

            # Send completion event with checksum
            send_event(
                "file_complete",
                {
                    "cmd_id": cmd_id,
                    "path": path,
                    "total_size": file_size,
                    "checksum": md5.hexdigest(),
                },
            )
            send_event("exit", {"cmd_id": cmd_id, "exit_code": 0})

    except Exception as e:
        send_event("error", {"cmd_id": cmd_id, "error": str(e)})
        send_event("exit", {"cmd_id": cmd_id, "exit_code": 1})


def handle_write_file(cmd_id, path, content, mode="wb", append=False):
    try:
        # Ensure directory exists
        dirname = os.path.dirname(path)
        if dirname:
            os.makedirs(dirname, exist_ok=True)

        decoded = base64.b64decode(content)

        file_mode = "ab" if append else "wb"

        with open(path, file_mode) as f:
            f.write(decoded)

        send_event("status", {"cmd_id": cmd_id, "status": "written"})
        send_event("exit", {"cmd_id": cmd_id, "exit_code": 0})

    except Exception as e:
        send_event("error", {"cmd_id": cmd_id, "error": str(e)})
        send_event("exit", {"cmd_id": cmd_id, "exit_code": 1})


def handle_vsock_upload(cmd_id, path: str, size: int, checksum: str):
    """Handles vsock-based file upload from host to guest.

    NOTE: This is a legacy function for host-initiated uploads.
    The new architecture uses guest-initiated connections where the guest
    would request the file. This function is kept for backward compatibility.

    Protocol (legacy host-initiated):
    1. Guest receives upload request with path, size, checksum
    2. Guest sends "ready" response via vsock
    3. Guest receives raw binary data until size bytes received
    4. Guest verifies checksum
    5. Guest sends "complete" or "error" response

    Args:
        cmd_id: Command ID for responses
        path: Destination path
        size: File size in bytes
        checksum: MD5 checksum for verification
    """
    global VSOCK_SOCKET

    try:
        # Ensure directory exists
        dirname = os.path.dirname(path)
        if dirname:
            os.makedirs(dirname, exist_ok=True)

        # Send ready response via vsock
        ready_msg = json.dumps({"type": "ready", "cmd_id": cmd_id}).encode() + b"\n"
        VSOCK_SOCKET.sendall(ready_msg)

        # Receive raw binary file data
        received_bytes = 0
        md5 = hashlib.md5()

        with open(path, "wb") as f:
            while received_bytes < size:
                remaining = size - received_bytes
                chunk_size = min(65536, remaining)
                chunk = VSOCK_SOCKET.recv(chunk_size)
                if not chunk:
                    raise Exception("Connection closed during upload")
                f.write(chunk)
                md5.update(chunk)
                received_bytes += len(chunk)

        # Verify checksum
        file_checksum = md5.hexdigest()
        if file_checksum == checksum:
            complete_msg = (
                json.dumps(
                    {"type": "complete", "cmd_id": cmd_id, "size": received_bytes}
                ).encode()
                + b"\n"
            )
            VSOCK_SOCKET.sendall(complete_msg)
            send_event("exit", {"cmd_id": cmd_id, "exit_code": 0})
        else:
            error_msg = (
                json.dumps(
                    {
                        "type": "error",
                        "cmd_id": cmd_id,
                        "error": f"Checksum mismatch: expected {checksum}, got {file_checksum}",
                    }
                ).encode()
                + b"\n"
            )
            VSOCK_SOCKET.sendall(error_msg)
            send_event("exit", {"cmd_id": cmd_id, "exit_code": 1})

    except Exception as e:
        try:
            if VSOCK_SOCKET:
                error_msg = (
                    json.dumps(
                        {"type": "error", "cmd_id": cmd_id, "error": str(e)}
                    ).encode()
                    + b"\n"
                )
                VSOCK_SOCKET.sendall(error_msg)
        except:
            pass
        send_event("exit", {"cmd_id": cmd_id, "exit_code": 1})


def handle_vsock_download(cmd_id, path: str):
    """Sends a file from guest to host via vsock (guest-initiated upload).

    This function is triggered by a read_file request from the host. The guest
    connects to the host's vsock listener and uploads the requested file.

    Protocol:
    1. Host sends read_file request via serial console
    2. Guest connects to host vsock listener at BANDSOX_VSOCK_PORT
    3. Guest sends "upload" request with path, size, checksum, cmd_id
    4. Host sends "ready" response
    5. Guest sends raw binary file data
    6. Host verifies checksum and sends "complete" or "error"

    If vsock connection fails, falls back to serial console transfer.

    Args:
        cmd_id: Command ID for responses (used by host to route file to correct destination)
        path: Source file path in guest
    """
    vsock_port = int(os.environ.get("BANDSOX_VSOCK_PORT", "9000"))
    sock = None

    try:
        # Check file exists
        if not os.path.exists(path):
            send_event("error", {"cmd_id": cmd_id, "error": f"File not found: {path}"})
            send_event("exit", {"cmd_id": cmd_id, "exit_code": 1})
            return

        # Get file info
        file_size = os.path.getsize(path)

        # Calculate checksum
        md5 = hashlib.md5()
        with open(path, "rb") as f:
            while True:
                chunk = f.read(65536)
                if not chunk:
                    break
                md5.update(chunk)
        checksum = md5.hexdigest()

        # Connect to host
        sock = vsock_create_connection(vsock_port)
        if not sock:
            sys.stderr.write(
                f"WARNING: Vsock connection failed, falling back to serial\n"
            )
            sys.stderr.flush()
            # Fall back to serial
            handle_read_file(cmd_id, path)
            return

        # Send upload request (we're uploading to the host)
        request = {
            "type": "upload",
            "path": path,
            "size": file_size,
            "checksum": checksum,
            "cmd_id": cmd_id,
        }
        vsock_send_json_msg(sock, request)

        # Wait for ready response
        response = vsock_recv_json_msg(sock)
        if response.get("type") == "error":
            raise Exception(response.get("error", "Unknown error"))
        if response.get("type") != "ready":
            raise Exception(f"Unexpected response: {response.get('type')}")

        # Send file data
        with open(path, "rb") as f:
            while True:
                chunk = f.read(65536)
                if not chunk:
                    break
                sock.sendall(chunk)

        # Wait for complete response
        response = vsock_recv_json_msg(sock)
        if response.get("type") == "error":
            raise Exception(response.get("error", "Unknown error"))
        if response.get("type") != "complete":
            raise Exception(f"Unexpected response: {response.get('type')}")

        sys.stderr.write(f"INFO: File uploaded via vsock: {path} ({file_size} bytes)\n")
        sys.stderr.flush()

        send_event(
            "status", {"cmd_id": cmd_id, "status": "uploaded", "size": file_size}
        )
        send_event("exit", {"cmd_id": cmd_id, "exit_code": 0})

    except Exception as e:
        sys.stderr.write(f"ERROR: Vsock upload failed: {e}\n")
        sys.stderr.flush()
        send_event("error", {"cmd_id": cmd_id, "error": f"Vsock upload failed: {e}"})
        send_event("exit", {"cmd_id": cmd_id, "exit_code": 1})

    finally:
        if sock:
            try:
                sock.close()
            except:
                pass


def handle_vsock_download_from_host(cmd_id, path: str, dest_path: str = None):
    """Downloads a file FROM the host TO the guest via vsock (guest-initiated).

    This is for when the guest needs to receive a file from the host.

    Protocol:
    1. Guest connects to host vsock listener
    2. Guest sends "download" request with path
    3. Host sends file chunks
    4. Host sends "complete" with checksum
    5. Guest verifies and saves file

    Args:
        cmd_id: Command ID for responses
        path: Source file path (on host)
        dest_path: Destination path (on guest), defaults to same as path
    """
    if dest_path is None:
        dest_path = path

    vsock_port = int(os.environ.get("BANDSOX_VSOCK_PORT", "9000"))
    sock = None

    try:
        # Connect to host
        sock = vsock_create_connection(vsock_port)
        if not sock:
            raise Exception("Failed to connect to vsock")

        # Send download request
        request = {
            "type": "download",
            "path": path,
            "cmd_id": cmd_id,
        }
        vsock_send_json_msg(sock, request)

        # Ensure directory exists
        dirname = os.path.dirname(dest_path)
        if dirname:
            os.makedirs(dirname, exist_ok=True)

        # Receive file chunks
        md5 = hashlib.md5()
        total_received = 0

        with open(dest_path, "wb") as f:
            while True:
                response = vsock_recv_json_msg(sock)
                resp_type = response.get("type")

                if resp_type == "error":
                    raise Exception(response.get("error", "Unknown error"))

                elif resp_type == "chunk":
                    data = base64.b64decode(response.get("data", ""))
                    f.write(data)
                    md5.update(data)
                    total_received += len(data)

                elif resp_type == "complete":
                    # Verify checksum
                    expected_checksum = response.get("checksum")
                    if expected_checksum:
                        actual_checksum = md5.hexdigest()
                        if actual_checksum != expected_checksum:
                            raise Exception(
                                f"Checksum mismatch: expected {expected_checksum}, got {actual_checksum}"
                            )
                    break
                else:
                    raise Exception(f"Unexpected response type: {resp_type}")

        sys.stderr.write(
            f"INFO: File downloaded via vsock: {path} -> {dest_path} ({total_received} bytes)\n"
        )
        sys.stderr.flush()

        send_event(
            "status", {"cmd_id": cmd_id, "status": "downloaded", "size": total_received}
        )
        send_event("exit", {"cmd_id": cmd_id, "exit_code": 0})

    except Exception as e:
        sys.stderr.write(f"ERROR: Vsock download failed: {e}\n")
        sys.stderr.flush()
        send_event("error", {"cmd_id": cmd_id, "error": f"Vsock download failed: {e}"})
        send_event("exit", {"cmd_id": cmd_id, "exit_code": 1})

    finally:
        if sock:
            try:
                sock.close()
            except:
                pass


def handle_file_info(cmd_id, path):
    try:
        if not os.path.exists(path):
            send_event("error", {"cmd_id": cmd_id, "error": f"Path not found: {path}"})
            send_event("exit", {"cmd_id": cmd_id, "exit_code": 1})
            return

        stat_info = os.stat(path)
        send_event(
            "status",
            {
                "cmd_id": cmd_id,
                "size": stat_info.st_size,
                "mode": oct(stat_info.st_mode),
                "mtime": stat_info.st_mtime,
            },
        )
        send_event("exit", {"cmd_id": cmd_id, "exit_code": 0})
    except Exception as e:
        send_event("error", {"cmd_id": cmd_id, "error": str(e)})
        send_event("exit", {"cmd_id": cmd_id, "exit_code": 1})


def handle_list_dir(cmd_id, path):
    try:
        if not os.path.exists(path):
            send_event("error", {"cmd_id": cmd_id, "error": f"Path not found: {path}"})
            send_event("exit", {"cmd_id": cmd_id, "exit_code": 1})
            return

        files = []
        try:
            with os.scandir(path) as it:
                for entry in it:
                    try:
                        stat = entry.stat()
                        files.append(
                            {
                                "name": entry.name,
                                "type": "directory" if entry.is_dir() else "file",
                                "size": stat.st_size,
                                "mode": stat.st_mode,
                                "mtime": stat.st_mtime,
                            }
                        )
                    except OSError:
                        # Handle cases where stat fails (broken links etc)
                        files.append({"name": entry.name, "type": "unknown", "size": 0})
        except NotADirectoryError:
            send_event("error", {"cmd_id": cmd_id, "error": f"Not a directory: {path}"})
            send_event("exit", {"cmd_id": cmd_id, "exit_code": 1})
            return

        send_event("dir_list", {"cmd_id": cmd_id, "path": path, "files": files})
        send_event("exit", {"cmd_id": cmd_id, "exit_code": 0})

    except Exception as e:
        send_event("error", {"cmd_id": cmd_id, "error": str(e)})
        send_event("exit", {"cmd_id": cmd_id, "exit_code": 1})


def main():
    # Ensure stdout is line buffered or unbuffered
    # sys.stdout.reconfigure(line_buffering=True) # Python 3.7+

    send_event("status", {"status": "ready"})

    while True:
        try:
            line = sys.stdin.readline()
            if not line:
                break

            try:
                req = json.loads(line)
                req_type = req.get(
                    "type", "exec"
                )  # Default to exec for backward compat
                cmd_id = req.get("id")

                if req_type == "exec":
                    cmd = req.get("command")
                    bg = req.get("background", False)
                    env = req.get("env")
                    if cmd:
                        # Run in a thread to allow concurrent commands/sessions
                        t = threading.Thread(
                            target=handle_command,
                            args=(cmd_id, cmd, bg, env),
                            daemon=True,
                        )
                        t.start()
                    else:
                        send_event("error", {"error": "Invalid request"})

                elif req_type == "pty_exec":
                    cmd = req.get("command")
                    cols = req.get("cols", 80)
                    rows = req.get("rows", 24)
                    env = req.get("env")
                    t = threading.Thread(
                        target=handle_pty_command,
                        args=(cmd_id, cmd, cols, rows, env),
                        daemon=True,
                    )
                    t.start()

                elif req_type == "input":
                    data = req.get("data")
                    encoding = req.get("encoding")
                    handle_input(cmd_id, data, encoding)

                elif req_type == "resize":
                    cols = req.get("cols", 80)
                    rows = req.get("rows", 24)
                    handle_resize(cmd_id, cols, rows)

                elif req_type == "kill":
                    handle_kill(cmd_id)

                elif req_type == "read_file":
                    path = req.get("path")
                    vsock_port = int(os.environ.get("BANDSOX_VSOCK_PORT", "9000"))

                    # Try vsock first - handle_vsock_download has built-in fallback to serial
                    if vsock_check_available():
                        t = threading.Thread(
                            target=handle_vsock_download,
                            args=(cmd_id, path),
                            daemon=True,
                        )
                        t.start()
                    else:
                        # No vsock support, use serial directly
                        sys.stderr.write(
                            "INFO: Vsock not available, using serial for read_file\n"
                        )
                        sys.stderr.flush()
                        t = threading.Thread(
                            target=handle_read_file, args=(cmd_id, path), daemon=True
                        )
                        t.start()

                elif req_type == "file_info":
                    path = req.get("path")
                    # file_info currently doesn't use vsock, always use serial
                    t = threading.Thread(
                        target=handle_file_info, args=(cmd_id, path), daemon=True
                    )
                    t.start()

                elif req_type == "write_file":
                    path = req.get("path")
                    content = req.get("content")
                    append = req.get("append", False)

                    # Content is already in the request (sent via serial/multiplexer)
                    # Use handle_write_file directly - vsock upload is only for
                    # explicit vsock_upload requests where data streams via vsock
                    t = threading.Thread(
                        target=handle_write_file,
                        args=(cmd_id, path, content, "wb", append),
                        daemon=True,
                    )
                    t.start()

                elif req_type == "list_dir":
                    path = req.get("path")
                    t = threading.Thread(
                        target=handle_list_dir, args=(cmd_id, path), daemon=True
                    )
                    t.start()

            except json.JSONDecodeError:
                # Ignore noise
                pass

        except KeyboardInterrupt:
            break


if __name__ == "__main__":
    main()
