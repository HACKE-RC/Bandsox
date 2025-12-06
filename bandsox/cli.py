import argparse
import uvicorn
import os
import sys
import threading
import json
import base64
import signal
import shutil
import termios
import tty
import fcntl
import struct

def terminal_client(vm_id, host, port):
    try:
        from websockets.sync.client import connect
    except ImportError:
        print("Error: 'websockets' library is required. Please install it.")
        return

    cols, rows = shutil.get_terminal_size()
    url = f"ws://{host}:{port}/api/vms/{vm_id}/terminal?cols={cols}&rows={rows}"
    
    try:
        # Increase open_timeout to allow time for server to wait for agent readiness
        with connect(url, open_timeout=30) as websocket:
            fd = sys.stdin.fileno()
            old_settings = termios.tcgetattr(fd)
            
            stop_event = threading.Event()
            
            def on_resize(signum, frame):
                c, r = shutil.get_terminal_size()
                try:
                    websocket.send(json.dumps({
                        "type": "resize",
                        "cols": c,
                        "rows": r
                    }))
                except Exception:
                    pass

            signal.signal(signal.SIGWINCH, on_resize)
            
            def reader():
                try:
                    while not stop_event.is_set():
                        try:
                            message = websocket.recv()
                            # message is base64 encoded
                            decoded = base64.b64decode(message)
                            sys.stdout.buffer.write(decoded)
                            sys.stdout.buffer.flush()
                        except Exception:
                            break
                finally:
                    stop_event.set()
                    # Restore terminal settings if reader fails
                    termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
                    os._exit(0) # Force exit to kill main thread input block

            t = threading.Thread(target=reader, daemon=True)
            t.start()
            
            try:
                tty.setraw(fd)
                while not stop_event.is_set():
                    data = sys.stdin.buffer.read(1)
                    if not data:
                        break
                    
                    encoded = base64.b64encode(data).decode('utf-8')
                    websocket.send(json.dumps({
                        "type": "input",
                        "data": encoded
                    }))
            except Exception:
                pass
            finally:
                termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
                stop_event.set()
                
    except Exception as e:
        print(f"Connection failed: {e}")
        # Try to print more details if it's a ConnectionClosed
        if hasattr(e, 'code'):
            print(f"Close code: {e.code}")
        if hasattr(e, 'reason'):
            print(f"Close reason: {e.reason}")

def download_kernel(output_path="vmlinux"):
    url = "https://s3.amazonaws.com/spec.ccfc.min/img/quickstart_guide/x86_64/kernels/vmlinux.bin"
    import requests
    print(f"Downloading kernel from {url}...")
    try:
        response = requests.get(url, stream=True)
        response.raise_for_status()
        total_size = int(response.headers.get('content-length', 0))
        block_size = 8192
        downloaded = 0
        
        with open(output_path, "wb") as f:
            for chunk in response.iter_content(chunk_size=block_size):
                downloaded += len(chunk)
                f.write(chunk)
                # Simple progress
                if total_size > 0:
                    percent = int(downloaded / total_size * 100)
                    print(f"Downloading: {percent}%", end="\r")
        print("\nKernel downloaded successfully.")
        return True
    except Exception as e:
        print(f"\nFailed to download kernel: {e}")
        return False

def main():
    parser = argparse.ArgumentParser(description="BandSox CLI")
    subparsers = parser.add_subparsers(dest="command")
    
    serve_parser = subparsers.add_parser("serve", help="Start the web dashboard")
    serve_parser.add_argument("--port", type=int, default=8000, help="Port to listen on")
    serve_parser.add_argument("--host", type=str, default="0.0.0.0", help="Host to listen on")
    
    term_parser = subparsers.add_parser("terminal", help="Open a terminal session in a VM")
    term_parser.add_argument("vm_id", type=str, help="VM ID")
    term_parser.add_argument("--host", type=str, default="127.0.0.1", help="Host to connect to")
    term_parser.add_argument("--port", type=int, default=8000, help="Port to connect to")

    create_parser = subparsers.add_parser("create", help="Create a new VM")
    create_parser.add_argument("image", type=str, help="Docker image to use")
    create_parser.add_argument("--name", type=str, help="VM name")
    create_parser.add_argument("--host", type=str, default="127.0.0.1", help="Host to connect to")
    create_parser.add_argument("--port", type=int, default=8000, help="Port to connect to")
    create_parser.add_argument("--vcpu", type=int, default=1, help="Number of vCPUs")
    create_parser.add_argument("--mem", type=int, default=128, help="Memory in MiB")
    
    init_parser = subparsers.add_parser("init", help="Initialize environment (download kernel)")
    init_parser.add_argument("--output", type=str, default="vmlinux", help="Output path for kernel")
    
    args = parser.parse_args()
    
    if args.command == "serve":
        print(f"Starting dashboard at http://{args.host}:{args.port}")
        uvicorn.run("bandsox.server:app", host=args.host, port=args.port, reload=True)
    elif args.command == "terminal":
        terminal_client(args.vm_id, args.host, args.port)
    elif args.command == "create":
        import requests
        try:
            url = f"http://{args.host}:{args.port}/api/vms"
            payload = {"image": args.image}
            if args.name:
                payload["name"] = args.name
            payload["vcpu"] = args.vcpu
            payload["mem_mib"] = args.mem
            
            resp = requests.post(url, json=payload)
            if resp.status_code == 200:
                print(f"VM created: {resp.json()['id']}")
            else:
                print(f"Failed to create VM: {resp.text}")
        except Exception as e:
            print(f"Error: {e}")
    elif args.command == "init":
        download_kernel(args.output)
    else:
        parser.print_help()

if __name__ == "__main__":
    main()
