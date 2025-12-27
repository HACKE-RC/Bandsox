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
import requests
import tarfile
import tempfile
from pathlib import Path
from urllib.parse import urlparse

def _stringify(value):
    return "" if value is None else str(value)

def _shrink_widths(col_widths, max_width, min_width=6):
    """Shrink column widths to fit max_width, prioritizing wider columns."""
    total = sum(col_widths) + 3 * (len(col_widths) - 1)  # account for separators
    if total <= max_width:
        return col_widths
    
    # Keep shrinking the widest columns until we fit or hit min_width
    widths = col_widths[:]
    while total > max_width:
        widest_idx = max(range(len(widths)), key=lambda i: widths[i])
        if widths[widest_idx] <= min_width:
            break
        widths[widest_idx] -= 1
        total = sum(widths) + 3 * (len(widths) - 1)
    return widths

def _truncate(val, width):
    s = _stringify(val)
    if len(s) <= width:
        return s
    if width <= 1:
        return s[:width]
    if width == 2:
        return s[:1] + "…"
    return s[: width - 1] + "…"

def _format_table(rows, headers, max_width=None):
    """Return a simple aligned table string without extra deps, width-aware."""
    if not rows:
        return ""
    
    col_widths = [len(h) for h in headers]
    for row in rows:
        for idx, cell in enumerate(row):
            col_widths[idx] = max(col_widths[idx], len(_stringify(cell)))
    
    if max_width:
        col_widths = _shrink_widths(col_widths, max_width)
    
    def fmt_row(row_vals):
        cells = []
        for idx, val in enumerate(row_vals):
            truncated = _truncate(val, col_widths[idx])
            cells.append(truncated.ljust(col_widths[idx]))
        return " | ".join(cells)
    
    divider = "-+-".join("-" * w for w in col_widths)
    lines = [fmt_row(headers), divider]
    lines.extend(fmt_row(row) for row in rows)
    return "\n".join(lines)

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

DEFAULT_KERNEL_URL = "https://s3.amazonaws.com/spec.ccfc.min/img/quickstart_guide/x86_64/kernels/vmlinux.bin"
DEFAULT_CNI_URL = "https://github.com/containernetworking/plugins/releases/download/v1.5.1/cni-plugins-linux-amd64-v1.5.1.tgz"


def _stream_download(url, output_path, label, force=False):
    """Fetch a URL or local file to disk with a small progress indicator."""
    output_path = Path(output_path)
    if output_path.exists() and not force:
        print(f"Skipping {label}: {output_path} already exists (use --force to re-download).")
        return True
    output_path.parent.mkdir(parents=True, exist_ok=True)

    parsed = urlparse(str(url))
    is_http = parsed.scheme in ("http", "https")
    is_file_scheme = parsed.scheme == "file"
    is_local_path = parsed.scheme == "" and Path(url).expanduser().exists()

    if is_file_scheme or is_local_path:
        src = Path(parsed.path if is_file_scheme else url).expanduser().resolve()
        if not src.exists():
            print(f"Failed to copy {label}: source {src} not found.")
            return False
        if src == output_path:
            print(f"{label} already at destination ({output_path}).")
            return True
        try:
            shutil.copyfile(src, output_path)
            print(f"{label} copied from {src} to {output_path}")
            return True
        except Exception as e:
            print(f"Failed to copy {label} from {src}: {e}")
            return False

    if not is_http:
        print(f"Invalid URL or path for {label}: {url}")
        return False

    print(f"Downloading {label} from {url} -> {output_path}")
    try:
        response = requests.get(url, stream=True)
        response.raise_for_status()
        total_size = int(response.headers.get("content-length", 0))
        block_size = 8192
        downloaded = 0

        with open(output_path, "wb") as f:
            for chunk in response.iter_content(chunk_size=block_size):
                if not chunk:
                    continue
                downloaded += len(chunk)
                f.write(chunk)
                if total_size > 0:
                    percent = int(downloaded / total_size * 100)
                    print(f"Downloading {label}: {percent}%", end="\r")
        if total_size > 0:
            print()
        print(f"{label} downloaded to {output_path}")
        return True
    except Exception as e:
        print(f"Failed to download {label}: {e}")
        return False


def download_kernel(output_path="vmlinux", kernel_url=DEFAULT_KERNEL_URL, force=False):
    return _stream_download(kernel_url, output_path, "kernel", force=force)


def download_cni_plugins(url=DEFAULT_CNI_URL, dest_dir="cni/bin", force=False):
    dest = Path(dest_dir)
    if dest.exists() and any(dest.iterdir()) and not force:
        print(f"Skipping CNI plugins: {dest} already populated (use --force to re-download).")
        return True

    dest.mkdir(parents=True, exist_ok=True)
    print(f"Downloading CNI plugins from {url}")
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".tgz") as tmp:
            tmp_path = Path(tmp.name)
        ok = _stream_download(url, tmp_path, "CNI plugins", force=True)
        if not ok:
            tmp_path.unlink(missing_ok=True)
            return False

        with tarfile.open(tmp_path, "r:gz") as tar:
            tar.extractall(dest)
        tmp_path.unlink(missing_ok=True)
        print(f"CNI plugins extracted to {dest}")
        return True
    except Exception as e:
        print(f"Failed to download/extract CNI plugins: {e}")
        return False


def download_rootfs(url, output_path="storage/images/bandsox-base.ext4", force=False):
    return _stream_download(url, output_path, "rootfs image", force=force)

def cleanup_taps():
    """Cleans up stale TAP devices."""
    print("Cleaning up stale TAP devices...")
    import subprocess
    try:
        # List all tap devices
        result = subprocess.run(["ip", "link", "show"], capture_output=True, text=True)
        if result.returncode != 0:
            print("Failed to list interfaces.")
            return

        taps = []
        for line in result.stdout.splitlines():
            # Format: 123: tapXXXX: <...>
            parts = line.split(": ")
            if len(parts) >= 2:
                iface = parts[1].split("@")[0] # handle veth@ifX
                if iface.startswith("tap"):
                    taps.append(iface)
        
        if not taps:
            print("No TAP devices found.")
            return

        for tap in taps:
            print(f"Deleting {tap}...")
            subprocess.run(["sudo", "ip", "link", "delete", tap])
            
        print(f"Cleaned up {len(taps)} devices.")
            
    except Exception as e:
        print(f"Error during cleanup: {e}")

def main():
    parser = argparse.ArgumentParser(description="BandSox CLI")
    subparsers = parser.add_subparsers(dest="command")
    
    serve_parser = subparsers.add_parser("serve", help="Start the web dashboard")
    serve_parser.add_argument("--port", type=int, default=8000, help="Port to listen on")
    serve_parser.add_argument("--host", type=str, default="0.0.0.0", help="Host to listen on")
    serve_parser.add_argument(
        "--storage",
        type=str,
        help="Path to storage directory (default: /var/lib/sandbox)",
    )
    
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
    create_parser.add_argument("--disk-size", type=int, default=4096, help="Disk size in MiB")

    vm_parser = subparsers.add_parser("vm", help="Manage VMs")
    vm_sub = vm_parser.add_subparsers(dest="vm_command")
    
    vm_list = vm_sub.add_parser("list", help="List VMs")
    vm_list.add_argument("--host", type=str, default="127.0.0.1", help="Host to connect to")
    vm_list.add_argument("--port", type=int, default=8000, help="Port to connect to")

    vm_stop = vm_sub.add_parser("stop", help="Stop a VM")
    vm_stop.add_argument("vm_id", type=str, help="VM ID")
    vm_stop.add_argument("--host", type=str, default="127.0.0.1", help="Host to connect to")
    vm_stop.add_argument("--port", type=int, default=8000, help="Port to connect to")

    vm_pause = vm_sub.add_parser("pause", help="Pause a VM")
    vm_pause.add_argument("vm_id", type=str, help="VM ID")
    vm_pause.add_argument("--host", type=str, default="127.0.0.1", help="Host to connect to")
    vm_pause.add_argument("--port", type=int, default=8000, help="Port to connect to")

    vm_resume = vm_sub.add_parser("resume", help="Resume a paused VM")
    vm_resume.add_argument("vm_id", type=str, help="VM ID")
    vm_resume.add_argument("--host", type=str, default="127.0.0.1", help="Host to connect to")
    vm_resume.add_argument("--port", type=int, default=8000, help="Port to connect to")

    vm_delete = vm_sub.add_parser("delete", help="Delete a VM")
    vm_delete.add_argument("vm_id", type=str, help="VM ID")
    vm_delete.add_argument("--host", type=str, default="127.0.0.1", help="Host to connect to")
    vm_delete.add_argument("--port", type=int, default=8000, help="Port to connect to")

    vm_save = vm_sub.add_parser("save", help="Snapshot a running VM")
    vm_save.add_argument("vm_id", type=str, help="VM ID to snapshot")
    vm_save.add_argument("name", type=str, help="Snapshot name")
    vm_save.add_argument("--host", type=str, default="127.0.0.1", help="Host to connect to")
    vm_save.add_argument("--port", type=int, default=8000, help="Port to connect to")

    snap_parser = subparsers.add_parser("snapshot", help="Manage snapshots")
    snap_sub = snap_parser.add_subparsers(dest="snapshot_command")

    snap_list = snap_sub.add_parser("list", help="List snapshots")
    snap_list.add_argument("--host", type=str, default="127.0.0.1", help="Host to connect to")
    snap_list.add_argument("--port", type=int, default=8000, help="Port to connect to")

    snap_delete = snap_sub.add_parser("delete", help="Delete a snapshot")
    snap_delete.add_argument("snapshot_id", type=str, help="Snapshot ID")
    snap_delete.add_argument("--host", type=str, default="127.0.0.1", help="Host to connect to")
    snap_delete.add_argument("--port", type=int, default=8000, help="Port to connect to")

    snap_restore = snap_sub.add_parser("restore", help="Restore a snapshot into a new VM")
    snap_restore.add_argument("snapshot_id", type=str, help="Snapshot ID")
    snap_restore.add_argument("--name", type=str, help="Name for the restored VM")
    snap_restore.add_argument("--enable-networking", action="store_true", default=True, help="Enable networking in restored VM")
    snap_restore.add_argument("--host", type=str, default="127.0.0.1", help="Host to connect to")
    snap_restore.add_argument("--port", type=int, default=8000, help="Port to connect to")
    
    cleanup_parser = subparsers.add_parser("cleanup", help="Cleanup stale resources (TAP devices)")

    init_parser = subparsers.add_parser("init", help="Initialize environment (download kernel/CNI/rootfs)")
    init_parser.add_argument("--kernel-url", type=str, default=DEFAULT_KERNEL_URL, help="URL to download kernel")
    init_parser.add_argument("--kernel-output", type=str, default="vmlinux", help="Output path for kernel")
    init_parser.add_argument("--cni-url", type=str, default=DEFAULT_CNI_URL, help="URL to download CNI plugin bundle (tgz)")
    init_parser.add_argument("--cni-dir", type=str, default="cni/bin", help="Destination directory for CNI plugins")
    init_parser.add_argument("--rootfs-url", type=str, default=None, help="Optional URL to download a base rootfs (.ext4)")
    init_parser.add_argument("--rootfs-output", type=str, default="storage/images/bandsox-base.ext4", help="Output path for base rootfs")
    init_parser.add_argument("--skip-kernel", action="store_true", help="Skip kernel download")
    init_parser.add_argument("--skip-cni", action="store_true", help="Skip CNI plugins download")
    init_parser.add_argument("--skip-rootfs", action="store_true", help="Skip rootfs download")
    init_parser.add_argument("--force", action="store_true", help="Re-download artifacts even if they exist")
    
    args = parser.parse_args()
    
    if args.command == "serve":
        if args.storage:
            storage_path = os.path.abspath(args.storage)
            print(f"Setting storage path to {storage_path}")
        else:
            storage_path = "/var/lib/sandbox"
            print(f"No --storage provided; defaulting to {storage_path}")
        os.environ["BANDSOX_STORAGE"] = storage_path

        print(f"Starting dashboard at http://{args.host}:{args.port}")
        uvicorn.run("bandsox.server:app", host=args.host, port=args.port, reload=True)
    elif args.command == "terminal":
        terminal_client(args.vm_id, args.host, args.port)
    elif args.command == "create":
        try:
            url = f"http://{args.host}:{args.port}/api/vms"
            payload = {"image": args.image}
            if args.name:
                payload["name"] = args.name
            payload["vcpu"] = args.vcpu
            payload["mem_mib"] = args.mem
            payload["disk_size_mib"] = args.disk_size
            
            resp = requests.post(url, json=payload)
            if resp.status_code == 200:
                print(f"VM created: {resp.json()['id']}")
            else:
                print(f"Failed to create VM: {resp.text}")
        except Exception as e:
            print(f"Error: {e}")
    elif args.command == "vm":
        if not args.vm_command:
            vm_parser.print_help()
            return
        base = f"http://{args.host}:{args.port}/api/vms"
        if args.vm_command == "list":
            try:
                resp = requests.get(base)
                if resp.status_code != 200:
                    print(f"Failed to list VMs ({resp.status_code}): {resp.text}")
                    return
                vms = resp.json()
                if not vms:
                    print("No VMs found.")
                    return
                rows = []
                for vm in vms:
                    rows.append([
                        vm.get("name") or "(unnamed)",
                        vm.get("status", "unknown"),
                        vm.get("id", "<unknown>"),
                        vm.get("image", "n/a"),
                    ])
                term_cols = shutil.get_terminal_size(fallback=(120, 20)).columns
                table = _format_table(
                    rows,
                    ["Name", "Status", "ID", "Image"],
                    max_width=term_cols,
                )
                print(table)
                print(f"\nTotal: {len(rows)} VM(s)")
            except Exception as e:
                print(f"Error: {e}")
        elif args.vm_command == "stop":
            url = f"{base}/{args.vm_id}/stop"
            try:
                resp = requests.post(url)
                if resp.status_code == 200:
                    print(f"VM {args.vm_id} stopped.")
                else:
                    print(f"Failed to stop VM {args.vm_id} ({resp.status_code}): {resp.text}")
            except Exception as e:
                print(f"Error: {e}")
        elif args.vm_command == "pause":
            url = f"{base}/{args.vm_id}/pause"
            try:
                resp = requests.post(url)
                if resp.status_code == 200:
                    print(f"VM {args.vm_id} paused.")
                else:
                    print(f"Failed to pause VM {args.vm_id} ({resp.status_code}): {resp.text}")
            except Exception as e:
                print(f"Error: {e}")
        elif args.vm_command == "resume":
            url = f"{base}/{args.vm_id}/resume"
            try:
                resp = requests.post(url)
                if resp.status_code == 200:
                    print(f"VM {args.vm_id} resumed.")
                else:
                    print(f"Failed to resume VM {args.vm_id} ({resp.status_code}): {resp.text}")
            except Exception as e:
                print(f"Error: {e}")
        elif args.vm_command == "delete":
            url = f"{base}/{args.vm_id}"
            try:
                resp = requests.delete(url)
                if resp.status_code == 200:
                    print(f"VM {args.vm_id} deleted.")
                else:
                    print(f"Failed to delete VM {args.vm_id} ({resp.status_code}): {resp.text}")
            except Exception as e:
                print(f"Error: {e}")
        elif args.vm_command == "save":
            url = f"{base}/{args.vm_id}/snapshot"
            try:
                resp = requests.post(url, json={"name": args.name})
                if resp.status_code == 200:
                    data = resp.json()
                    snap_id = data.get("snapshot_id", "<unknown>")
                    print(f"Snapshot created: {snap_id} (name={args.name})")
                else:
                    print(f"Failed to snapshot VM {args.vm_id} ({resp.status_code}): {resp.text}")
            except Exception as e:
                print(f"Error: {e}")
        else:
            vm_parser.print_help()
    elif args.command == "snapshot":
        if not args.snapshot_command:
            snap_parser.print_help()
            return
        base = f"http://{args.host}:{args.port}/api/snapshots"
        if args.snapshot_command == "list":
            try:
                resp = requests.get(base)
                if resp.status_code != 200:
                    print(f"Failed to list snapshots ({resp.status_code}): {resp.text}")
                    return
                snaps = resp.json()
                if not snaps:
                    print("No snapshots found.")
                    return
                rows = []
                for snap in snaps:
                    rows.append([
                        snap.get("name") or snap.get("snapshot_name") or "(unnamed)",
                        snap.get("id", "<unknown>"),
                        snap.get("status", "unknown"),
                    ])
                term_cols = shutil.get_terminal_size(fallback=(120, 20)).columns
                table = _format_table(
                    rows,
                    ["Name", "ID", "Status"],
                    max_width=term_cols,
                )
                print(table)
                print(f"\nTotal: {len(rows)} snapshot(s)")
            except Exception as e:
                print(f"Error: {e}")
        elif args.snapshot_command == "delete":
            url = f"{base}/{args.snapshot_id}"
            try:
                resp = requests.delete(url)
                if resp.status_code == 200:
                    print(f"Snapshot {args.snapshot_id} deleted.")
                else:
                    print(f"Failed to delete snapshot {args.snapshot_id} ({resp.status_code}): {resp.text}")
            except Exception as e:
                print(f"Error: {e}")
        elif args.snapshot_command == "restore":
            url = f"{base}/{args.snapshot_id}/restore"
            payload = {"name": args.name, "enable_networking": args.enable_networking}
            try:
                resp = requests.post(url, json=payload)
                if resp.status_code == 200:
                    data = resp.json()
                    new_id = data.get("id", "<unknown>")
                    print(f"Snapshot restored to VM: {new_id}")
                else:
                    print(f"Failed to restore snapshot {args.snapshot_id} ({resp.status_code}): {resp.text}")
            except Exception as e:
                print(f"Error: {e}")
        else:
            snap_parser.print_help()
    elif args.command == "cleanup":
        cleanup_taps()
    elif args.command == "init":
        if not args.skip_kernel:
            download_kernel(args.kernel_output, args.kernel_url, force=args.force)
        else:
            print("Skipping kernel download.")

        if not args.skip_cni:
            download_cni_plugins(args.cni_url, args.cni_dir, force=args.force)
        else:
            print("Skipping CNI plugins download.")

        if not args.skip_rootfs:
            if args.rootfs_url:
                download_rootfs(args.rootfs_url, args.rootfs_output, force=args.force)
            else:
                print("No --rootfs-url provided; skipping rootfs download.")
        else:
            print("Skipping rootfs download.")
    else:
        parser.print_help()

if __name__ == "__main__":
    main()
