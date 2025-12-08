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

# This agent runs inside the guest on ttyS0.
# It reads JSON commands from stdin and writes JSON events to stdout.

# Global session registry
sessions = {} # session_id -> process
pty_masters = {} # session_id -> master_fd

def send_event(event_type, payload):
    msg = json.dumps({"type": event_type, "payload": payload})
    sys.stdout.write(msg + "\n")
    sys.stdout.flush()

def read_stream(stream, stream_name, cmd_id):
    """Reads a stream line by line and sends events."""
    try:
        for line in stream:
            send_event("output", {
                "cmd_id": cmd_id,
                "stream": stream_name,
                "data": line
            })
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
                
                encoded = base64.b64encode(data).decode('utf-8')
                send_event("output", {
                    "cmd_id": cmd_id,
                    "stream": "stdout", # PTY combines stdout/stderr usually
                    "data": encoded,
                    "encoding": "base64"
                })
            except OSError as e:
                # EIO means PTY closed
                if e.errno == 5: # EIO
                    break
                # Other errors might be transient or fatal
                send_event("error", {"cmd_id": cmd_id, "error": f"PTY Read blocked: {e}"})
                break
    except Exception as e:
        send_event("error", {"cmd_id": cmd_id, "error": str(e)})

def handle_command(cmd_id, command, background=False):
    try:
        process = subprocess.Popen(
            command,
            shell=True,
            stdin=subprocess.PIPE, # Enable stdin
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1 # Line buffered
        )
        
        if background:
            sessions[cmd_id] = process
            
            # Start threads to read stdout/stderr
            t_out = threading.Thread(target=read_stream, args=(process.stdout, "stdout", cmd_id), daemon=True)
            t_err = threading.Thread(target=read_stream, args=(process.stderr, "stderr", cmd_id), daemon=True)
            t_out.start()
            t_err.start()
            
            # Monitor exit in a separate thread
            def monitor_exit():
                rc = process.wait()
                if cmd_id in sessions:
                    del sessions[cmd_id]
                send_event("exit", {
                    "cmd_id": cmd_id,
                    "exit_code": rc
                })
            
            t_mon = threading.Thread(target=monitor_exit, daemon=True)
            t_mon.start()
            
            send_event("status", {
                "cmd_id": cmd_id,
                "status": "started"
            })
            
        else:
            # Blocking execution (legacy)
            t_out = threading.Thread(target=read_stream, args=(process.stdout, "stdout", cmd_id))
            t_err = threading.Thread(target=read_stream, args=(process.stderr, "stderr", cmd_id))
            t_out.start()
            t_err.start()
            
            t_out.join()
            t_err.join()
            
            rc = process.wait()
            send_event("exit", {
                "cmd_id": cmd_id,
                "exit_code": rc
            })
        
    except Exception as e:
        send_event("error", {
            "cmd_id": cmd_id,
            "error": str(e)
        })

def handle_pty_command(cmd_id, command, cols=80, rows=24):
    try:
        pid, master_fd = pty.fork()
        
        if pid == 0:
            # Child process
            # Set window size
            winsize = struct.pack("HHHH", rows, cols, 0, 0)
            fcntl.ioctl(0, termios.TIOCSWINSZ, winsize)
            
            # Execute command
            # Use shell to execute command string
            args = ["/bin/sh", "-c", command]
            os.execvp(args[0], args)
            
        else:
            # Parent process
            pty_masters[cmd_id] = master_fd
            sessions[cmd_id] = pid # Store PID for PTY sessions
            
            # Start thread to read from master_fd
            t_read = threading.Thread(target=read_pty_master, args=(master_fd, cmd_id), daemon=True)
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
                    
                send_event("exit", {
                    "cmd_id": cmd_id,
                    "exit_code": exit_code
                })
                
            t_mon = threading.Thread(target=monitor_exit, daemon=True)
            t_mon.start()
            
            send_event("status", {
                "cmd_id": cmd_id,
                "status": "started"
            })
            
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
                    content = data.encode('utf-8')
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
                    send_event("error", {"cmd_id": cmd_id, "error": f"Write failed: {e}"})
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
    try:
        if not os.path.exists(path):
            send_event("error", {"cmd_id": cmd_id, "error": f"File not found: {path}"})
            send_event("exit", {"cmd_id": cmd_id, "exit_code": 1})
            return

        with open(path, "rb") as f:
            content = f.read()
            
        encoded = base64.b64encode(content).decode('utf-8')
        
        send_event("file_content", {
            "cmd_id": cmd_id,
            "path": path,
            "content": encoded
        })
        send_event("exit", {"cmd_id": cmd_id, "exit_code": 0})
        
    except Exception as e:
        send_event("error", {"cmd_id": cmd_id, "error": str(e)})
        send_event("exit", {"cmd_id": cmd_id, "exit_code": 1})

def handle_write_file(cmd_id, path, content, mode='wb', append=False):
    try:
        # Ensure directory exists
        dirname = os.path.dirname(path)
        if dirname:
            os.makedirs(dirname, exist_ok=True)
            
        decoded = base64.b64decode(content)
        
        file_mode = 'ab' if append else 'wb'
        
        with open(path, file_mode) as f:
            f.write(decoded)
            
        send_event("status", {
            "cmd_id": cmd_id,
            "status": "written"
        })
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
                        files.append({
                            "name": entry.name,
                            "type": "directory" if entry.is_dir() else "file", 
                            "size": stat.st_size,
                            "mode": stat.st_mode,
                            "mtime": stat.st_mtime
                        })
                    except OSError:
                         # Handle cases where stat fails (broken links etc)
                         files.append({
                            "name": entry.name,
                            "type": "unknown",
                            "size": 0
                         })
        except NotADirectoryError:
             send_event("error", {"cmd_id": cmd_id, "error": f"Not a directory: {path}"})
             send_event("exit", {"cmd_id": cmd_id, "exit_code": 1})
             return

        send_event("dir_list", {
            "cmd_id": cmd_id,
            "path": path,
            "files": files
        })
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
                req_type = req.get("type", "exec") # Default to exec for backward compat
                cmd_id = req.get("id")
                
                if req_type == "exec":
                    cmd = req.get("command")
                    bg = req.get("background", False)
                    if cmd:
                        # Run in a thread to allow concurrent commands/sessions
                        t = threading.Thread(target=handle_command, args=(cmd_id, cmd, bg), daemon=True)
                        t.start()
                    else:
                        send_event("error", {"error": "Invalid request"})

                elif req_type == "pty_exec":
                    cmd = req.get("command")
                    cols = req.get("cols", 80)
                    rows = req.get("rows", 24)
                    t = threading.Thread(target=handle_pty_command, args=(cmd_id, cmd, cols, rows), daemon=True)
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
                    t = threading.Thread(target=handle_read_file, args=(cmd_id, path), daemon=True)
                    t.start()

                elif req_type == "write_file":
                    path = req.get("path")
                    content = req.get("content")
                    append = req.get("append", False)
                    t = threading.Thread(target=handle_write_file, args=(cmd_id, path, content, 'wb', append), daemon=True)
                    t.start()

                elif req_type == "list_dir":
                    path = req.get("path")
                    t = threading.Thread(target=handle_list_dir, args=(cmd_id, path), daemon=True)
                    t.start()
                    
            except json.JSONDecodeError:
                # Ignore noise
                pass
                
        except KeyboardInterrupt:
            break

if __name__ == "__main__":
    main()
