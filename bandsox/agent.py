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

# Vsock Client Module
VSOCK_ENABLED = False
VSOCK_SOCKET = None
VSOCK_CID_HOST = 2  # Well-known CID for host


def vsock_connect(port: int, retry: int = 3) -> bool:
    """Connects to host vsock socket.

    Args:
        port: Port number to connect to
        retry: Number of connection attempts (default 3)

    Returns:
        True if connected, False if failed
    """
    global VSOCK_ENABLED, VSOCK_SOCKET

    try:
        socket.AF_VSOCK  # Will raise AttributeError if not available
    except AttributeError:
        sys.stderr.write(
            "WARNING: Vsock module not available, falling back to serial\n"
        )
        sys.stderr.flush()
        return False

    for attempt in range(retry):
        try:
            VSOCK_SOCKET = socket.socket(socket.AF_VSOCK, socket.SOCK_STREAM)
            VSOCK_SOCKET.settimeout(10)  # 10s connection timeout

            # Connect to host (CID=2) on specified port
            VSOCK_SOCKET.connect((VSOCK_CID_HOST, port))

            sys.stderr.write(
                f"INFO: Connected to vsock: CID={VSOCK_CID_HOST}, Port={port}\n"
            )
            sys.stderr.flush()
            VSOCK_ENABLED = True
            return True

        except Exception as e:
            if attempt < retry - 1:
                sys.stderr.write(
                    f"DEBUG: Vsock connection attempt {attempt + 1} failed: {e}\n"
                )
                sys.stderr.flush()
                time.sleep(1)  # 1s backoff
            else:
                sys.stderr.write(
                    f"WARNING: Vsock connection failed after {retry} attempts: {e}\n"
                )
                sys.stderr.flush()
                VSOCK_ENABLED = False
                return False

    return False


def vsock_send_json(data: dict):
    """Sends JSON data over vsock connection.

    Args:
        data: Dictionary to send as JSON
    """
    global VSOCK_SOCKET

    if not VSOCK_ENABLED or not VSOCK_SOCKET:
        raise Exception("Vsock not connected")

    message = json.dumps(data) + "\n"
    VSOCK_SOCKET.sendall(message.encode("utf-8"))


def vsock_read_line() -> str:
    """Reads a newline-delimited line from vsock connection.

    Returns:
        Complete line as string
    """
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
            line, buffer = buffer.split(b"\n", 1)
            return line.decode("utf-8")


def vsock_disconnect():
    """Disconnects from vsock socket and cleans up."""
    global VSOCK_SOCKET, VSOCK_ENABLED

    if VSOCK_SOCKET:
        VSOCK_SOCKET.close()
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

            send_event("status", {"cmd_id": cmd_id, "status": "started", "pid": process.pid})

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
            send_event("file_content", {"cmd_id": cmd_id, "path": path, "content": encoded})
            send_event("exit", {"cmd_id": cmd_id, "exit_code": 0})
        else:
            # Large file - send in chunks
            md5 = hashlib.md5()
            offset = 0
            
            with open(path, "rb") as f:
                while True:
                    chunk = f.read(CHUNK_SIZE)
                    if not chunk:
                        break
                    
                    md5.update(chunk)
                    encoded = base64.b64encode(chunk).decode("utf-8")
                    
                    send_event("file_chunk", {
                        "cmd_id": cmd_id,
                        "path": path,
                        "data": encoded,
                        "offset": offset,
                        "size": len(chunk)
                    })
                    
                    offset += len(chunk)
            
            # Send completion event with checksum
            send_event("file_complete", {
                "cmd_id": cmd_id,
                "path": path,
                "total_size": file_size,
                "checksum": md5.hexdigest()
            })
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
    """Handles vsock-based file upload (raw binary).

    Protocol:
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
    global vsock_socket
    
    try:
        # Ensure directory exists
        dirname = os.path.dirname(path)
        if dirname:
            os.makedirs(dirname, exist_ok=True)

        # Send ready response via vsock
        ready_msg = json.dumps({"type": "ready", "cmd_id": cmd_id}).encode() + b"\n"
        vsock_socket.sendall(ready_msg)

        # Receive raw binary file data
        received_bytes = 0
        md5 = hashlib.md5()
        
        with open(path, "wb") as f:
            while received_bytes < size:
                remaining = size - received_bytes
                chunk_size = min(65536, remaining)
                chunk = vsock_socket.recv(chunk_size)
                if not chunk:
                    raise Exception("Connection closed during upload")
                f.write(chunk)
                md5.update(chunk)
                received_bytes += len(chunk)

        # Verify checksum
        file_checksum = md5.hexdigest()
        if file_checksum == checksum:
            complete_msg = json.dumps({
                "type": "complete",
                "cmd_id": cmd_id,
                "size": received_bytes
            }).encode() + b"\n"
            vsock_socket.sendall(complete_msg)
            send_event("exit", {"cmd_id": cmd_id, "exit_code": 0})
        else:
            error_msg = json.dumps({
                "type": "error",
                "cmd_id": cmd_id,
                "error": f"Checksum mismatch: expected {checksum}, got {file_checksum}"
            }).encode() + b"\n"
            vsock_socket.sendall(error_msg)
            send_event("exit", {"cmd_id": cmd_id, "exit_code": 1})

    except Exception as e:
        try:
            error_msg = json.dumps({
                "type": "error",
                "cmd_id": cmd_id,
                "error": str(e)
            }).encode() + b"\n"
            vsock_socket.sendall(error_msg)
        except:
            pass
        send_event("exit", {"cmd_id": cmd_id, "exit_code": 1})


def handle_vsock_download(cmd_id, path: str):
    """Handles vsock-based file download.

    Protocol:
    1. Guest receives download request from host
    2. Guest reads file
    3. Guest sends file data in chunks with JSON metadata
    4. Guest sends "complete" response

    Args:
        cmd_id: Command ID for responses
        path: Source file path
    """
    try:
        if not os.path.exists(path):
            vsock_send_json(
                {
                    "type": "error",
                    "payload": {"cmd_id": cmd_id, "error": f"File not found: {path}"},
                }
            )
            vsock_disconnect()
            send_event("exit", {"cmd_id": cmd_id, "exit_code": 1})
            return

        file_size = os.path.getsize(path)

        # Read file and send in chunks
        chunk_size = 64 * 1024  # 64KB chunks
        bytes_sent = 0

        with open(path, "rb") as f:
            while bytes_sent < file_size:
                chunk = f.read(chunk_size)
                encoded = base64.b64encode(chunk).decode("utf-8")

                # Send chunk with metadata
                vsock_send_json(
                    {
                        "type": "status",
                        "payload": {
                            "cmd_id": cmd_id,
                            "type": "chunk",
                            "data": encoded,
                            "size": len(chunk),
                            "offset": bytes_sent,
                        },
                    }
                )

                bytes_sent += len(chunk)

        # Send complete response
        vsock_send_json(
            {
                "type": "status",
                "payload": {"cmd_id": cmd_id, "type": "complete", "size": file_size},
            }
        )
        vsock_disconnect()
        send_event("exit", {"cmd_id": cmd_id, "exit_code": 0})

    except Exception as e:
        vsock_send_json(
            {
                "type": "error",
                "payload": {"cmd_id": cmd_id, "error": f"Vsock download failed: {e}"},
            }
        )
        vsock_disconnect()
        send_event("exit", {"cmd_id": cmd_id, "exit_code": 1})


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

                    # Try vsock connection first
                    vsock_port = int(os.environ.get("BANDSOX_VSOCK_PORT", "9000"))
                    if vsock_connect(vsock_port):
                        # Vsock connected, use vsock handler
                        t = threading.Thread(
                            target=handle_vsock_download,
                            args=(cmd_id, path),
                            daemon=True,
                        )
                        t.start()
                    else:
                        # Fall back to serial
                        sys.stderr.write(
                            f"WARNING: Vsock connection failed for read_file, using serial\n"
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
