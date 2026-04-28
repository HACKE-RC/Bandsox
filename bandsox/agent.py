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
#
# IMPORTANT: stdout is the serial console. We must serialize writes so that
# concurrent worker threads don't interleave JSON lines on the wire (which
# corrupts framing on the host parser). The same applies to stderr: the
# Firecracker serial console is shared between stdout/stderr, and any
# unsynchronized write to stderr can interleave with an in-flight stdout
# JSON line. We send all diagnostic output through the same lock as events
# below, and we keep vsock diagnostics to a minimum to avoid swamping the
# serial channel with bookkeeping noise.
#
# Vsock Architecture (guest-initiated):
# The guest initiates a new AF_VSOCK connection for each file transfer.
# Firecracker forwards the connection to the host's Unix socket listener
# at <uds_path>_<port>. The host listener accepts, reads a JSON request,
# handles the transfer, and closes the connection. No shared state.
#
# Protocol:
# 1. Guest connects to host via AF_VSOCK(CID=2, port)
# 2. Guest sends a JSON request (upload or download)
# 3. Host sends "ready" (for upload) or chunks (for download)
# 4. Data is transferred
# 5. Connection closes when done

VSOCK_CID_HOST = 2  # Well-known CID for host


# Cached vsock availability. None = unknown, True = known-good (cached for
# the lifetime of the VM — once it works it keeps working), False = recently
# failed (reprobe after a short backoff).
#
# Early-boot races (listener not yet bound) must not permanently degrade
# the VM to serial for a full minute. We use a short initial reprobe that
# grows on repeat failures up to a cap.
_vsock_available_lock = threading.Lock()
_vsock_available = None  # type: ignore[assignment]
_vsock_last_probe_ts = 0.0
_vsock_fail_streak = 0
_VSOCK_REPROBE_BASE = 2.0  # seconds for first reprobe after a failure
_VSOCK_REPROBE_MAX = 30.0  # cap for repeated failures


def _vsock_module_available() -> bool:
    """Return True if the Python socket module has AF_VSOCK."""
    try:
        socket.AF_VSOCK  # noqa: B018 - attribute access is the test
        return True
    except AttributeError:
        return False


def _vsock_probe(port: int) -> bool:
    """Attempt a quick AF_VSOCK connection to verify vsock works."""
    global _vsock_available, _vsock_last_probe_ts, _vsock_fail_streak

    if not _vsock_module_available():
        with _vsock_available_lock:
            _vsock_available = False
            _vsock_last_probe_ts = time.time()
            _vsock_fail_streak += 1
        return False

    try:
        sock = socket.socket(socket.AF_VSOCK, socket.SOCK_STREAM)
        sock.settimeout(1.0)
        try:
            sock.connect((VSOCK_CID_HOST, port))
        finally:
            try:
                sock.close()
            except Exception:
                pass
        with _vsock_available_lock:
            _vsock_available = True
            _vsock_last_probe_ts = time.time()
            _vsock_fail_streak = 0
        return True
    except Exception:
        with _vsock_available_lock:
            _vsock_available = False
            _vsock_last_probe_ts = time.time()
            _vsock_fail_streak += 1
        return False


def _vsock_can_use(port: int) -> bool:
    """Return True if vsock is believed to work, probing on first call.

    Cached True means we keep using vsock. Cached False means we back
    off with exponential growth (2s, 4s, 8s, ... capped at 30s) so a
    one-shot early-boot race doesn't lock the VM into serial for long.
    """
    with _vsock_available_lock:
        state = _vsock_available
        since = time.time() - _vsock_last_probe_ts
        fails = _vsock_fail_streak

    if state is True:
        return True

    if state is False:
        # Exponential backoff: 2, 4, 8, 16, 30, 30, ...
        backoff = min(_VSOCK_REPROBE_BASE * (2 ** max(0, fails - 1)), _VSOCK_REPROBE_MAX)
        if since < backoff:
            return False

    # Unknown, or backoff elapsed → probe
    return _vsock_probe(port)


def _vsock_mark_broken():
    """Record that a vsock connection just failed so subsequent calls skip it."""
    global _vsock_available, _vsock_last_probe_ts
    with _vsock_available_lock:
        _vsock_available = False
        _vsock_last_probe_ts = time.time()


def vsock_create_connection(port: int, timeout: float = 10.0):
    """Create a new vsock connection to the host for a single transfer.

    Returns a connected socket or None if vsock is unavailable/broken.
    """
    if not _vsock_module_available():
        return None

    try:
        sock = socket.socket(socket.AF_VSOCK, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        sock.connect((VSOCK_CID_HOST, port))
        return sock
    except Exception:
        _vsock_mark_broken()
        return None


def vsock_send_json_msg(sock, data: dict):
    """Send a JSON message over a vsock connection (per-call, not shared)."""
    message = json.dumps(data) + "\n"
    sock.sendall(message.encode("utf-8"))


def vsock_recv_json_msg(sock) -> dict:
    """Receive a single newline-delimited JSON message from vsock."""
    buffer = b""
    while b"\n" not in buffer:
        chunk = sock.recv(4096)
        if not chunk:
            raise Exception("Vsock connection closed")
        buffer += chunk

    line, _ = buffer.split(b"\n", 1)
    return json.loads(line.decode("utf-8"))


# Global session registry
sessions = {}  # session_id -> process
pty_masters = {}  # session_id -> master_fd


# Single lock serializes ALL writes to the serial console (stdout and stderr).
# Without this, concurrent worker threads producing JSON events interleave
# bytes on the host's serial parser, which drops events and wedges commands.
# Historically we had a stdout-only lock; vsock reconnect chatter going to
# stderr was still able to interleave and corrupt the framing, so stderr
# writes now go through the same path.
_console_lock = threading.Lock()


def send_event(event_type, payload):
    msg = json.dumps({"type": event_type, "payload": payload})
    with _console_lock:
        sys.stdout.write(msg + "\n")
        sys.stdout.flush()


def log_stderr(msg: str):
    """Write a diagnostic line to stderr under the console lock.

    Keep calls to this sparse — each line costs bandwidth on the serial
    console and competes with real event traffic.
    """
    with _console_lock:
        sys.stderr.write(msg)
        if not msg.endswith("\n"):
            sys.stderr.write("\n")
        sys.stderr.flush()


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

                    send_event("file_chunk", {
                        "cmd_id": cmd_id,
                        "path": path,
                        "data": encoded,
                        "offset": offset,
                        "size": len(chunk)
                    })

                    offset += len(chunk)

                    # Throttle output to prevent serial buffer overflow
                    # Serial console is slow (~115200 baud = ~11KB/s max)
                    # 2KB chunk + base64 overhead = ~2.7KB, needs ~250ms to transmit
                    time.sleep(0.2)  # 200ms delay between chunks for serial safety

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


def handle_vsock_upload_to_host(cmd_id, path: str):
    """Uploads a file from guest to host via vsock (guest-initiated).

    Triggered by a read_file request from the host. The guest opens a new
    vsock connection, sends the upload request, streams file contents, then
    closes the connection. Each transfer uses its own short-lived socket so
    concurrent transfers don't fight over shared state.

    Falls back to serial (handle_read_file) on any vsock failure.
    """
    vsock_port = int(os.environ.get("BANDSOX_VSOCK_PORT", "9000"))
    sock = None

    try:
        if not os.path.exists(path):
            send_event("error", {"cmd_id": cmd_id, "error": f"File not found: {path}"})
            send_event("exit", {"cmd_id": cmd_id, "exit_code": 1})
            return

        file_size = os.path.getsize(path)

        # Compute checksum up front so the host can verify the stream
        md5 = hashlib.md5()
        with open(path, "rb") as f:
            while True:
                chunk = f.read(65536)
                if not chunk:
                    break
                md5.update(chunk)
        checksum = md5.hexdigest()

        sock = vsock_create_connection(vsock_port)
        if sock is None:
            # Vsock unavailable — silently fall back to serial. We do NOT write
            # a diagnostic here; that noise historically interleaved with real
            # events on the serial console and wedged the parser.
            handle_read_file(cmd_id, path)
            return

        # Send the upload request
        vsock_send_json_msg(
            sock,
            {
                "type": "upload",
                "path": path,
                "size": file_size,
                "checksum": checksum,
                "cmd_id": cmd_id,
            },
        )

        # Wait for ready
        response = vsock_recv_json_msg(sock)
        if response.get("type") == "error":
            raise Exception(response.get("error", "host error"))
        if response.get("type") != "ready":
            raise Exception(f"unexpected response type: {response.get('type')}")

        # Stream the file
        with open(path, "rb") as f:
            while True:
                chunk = f.read(65536)
                if not chunk:
                    break
                sock.sendall(chunk)

        # Wait for completion
        response = vsock_recv_json_msg(sock)
        if response.get("type") == "error":
            raise Exception(response.get("error", "host error"))
        if response.get("type") != "complete":
            raise Exception(f"unexpected response type: {response.get('type')}")

        # Tell the host side of the agent that the upload succeeded — the
        # VM.download_file caller uses this status event to know the file
        # was already written by the listener.
        send_event(
            "status",
            {"cmd_id": cmd_id, "status": "uploaded", "size": file_size},
        )
        send_event("exit", {"cmd_id": cmd_id, "exit_code": 0})

    except Exception as e:
        # Close the vsock socket before we fall back so we don't leak FDs.
        if sock is not None:
            try:
                sock.close()
            except Exception:
                pass
            sock = None

        # Treat failure as a signal that vsock is broken for this VM, at
        # least for the next minute — forces subsequent reads to serial
        # without another round of 1s connect timeouts.
        _vsock_mark_broken()
        # Fall back to the serial path so the caller still gets the file.
        handle_read_file(cmd_id, path)
        return
    finally:
        if sock is not None:
            try:
                sock.close()
            except Exception:
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

                    # Fast path: if we've already determined vsock is broken,
                    # go straight to serial without probing again.
                    if _vsock_can_use(vsock_port):
                        t = threading.Thread(
                            target=handle_vsock_upload_to_host,
                            args=(cmd_id, path),
                            daemon=True,
                        )
                        t.start()
                    else:
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
