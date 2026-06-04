"""Shared low-level helpers and constants for the MicroVM modules.

This lives apart from vm.py so the topical mixin modules (vm_files, vm_exec,
etc.) can import these without a circular dependency on vm.py, which imports
the mixins to assemble the MicroVM class. vm.py re-exports these names, so
existing `from .vm import kill_process_tree, DEFAULT_KERNEL_PATH` imports keep
working.
"""
import os
import json
import time
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_DIRECT_TEXT_WRITE_MAX_BYTES = 2 * 1024  # ~2 KiB
# Anything bigger goes through _write_bytes which prefers the fastwrite
# RPC (vsock). The previous 512 KiB threshold pushed athena's typical
# 5–50 KiB appends through the serial console — which deadlocked around
# the 4 KiB mark because of Firecracker's tiny UART FIFO. The host saw
# write() succeed, the guest never assembled a full JSON line, and the
# request timed out at 30s.
_SERIAL_WRITE_CHUNK_SIZE = 512 * 1024
_DEBUGFS_FULL_FILE_FALLBACK_LOG_THRESHOLD = 8 * 1024 * 1024


class FastIOError(Exception):
    """Structured error from FastRead/FastWrite RPC.

    Callers (athena UI / agent tools) inspect ``code`` to decide whether
    to retry transient failures (saturated, listener_down, timeout)
    versus surface a real problem to the user (not_found, too_large,
    bad_request, agent_error).
    """

    def __init__(self, code: str, msg: str):
        self.code = code
        self.msg = msg
        super().__init__(f"[{code}] {msg}")


def _parse_fastio_error(body: bytes) -> Exception:
    """Decode an error frame body into a FastIOError; tolerate old format."""
    try:
        obj = json.loads(body.decode("utf-8"))
        if isinstance(obj, dict) and "code" in obj:
            return FastIOError(obj.get("code", "internal"), obj.get("msg", ""))
    except Exception:
        pass
    return FastIOError("internal", body.decode("utf-8", errors="replace"))


def _pid_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _child_pids(pid: int) -> list:
    """Return direct child PIDs by scanning /proc."""
    children = []
    proc_dir = Path("/proc")
    if not proc_dir.exists():
        return children

    for entry in proc_dir.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            status = entry / "status"
            ppid = None
            with status.open() as f:
                for line in f:
                    if line.startswith("PPid:"):
                        ppid = int(line.split()[1])
                        break
            if ppid == pid:
                children.append(int(entry.name))
        except (FileNotFoundError, ProcessLookupError, PermissionError, ValueError):
            continue
    return children


def _descendant_pids(pid: int) -> list:
    """Return all descendant PIDs, children before grandchildren."""
    descendants = []
    queue = list(_child_pids(pid))
    while queue:
        child = queue.pop(0)
        descendants.append(child)
        queue.extend(_child_pids(child))
    return descendants


def kill_process_tree(pid: int, timeout: float = 1.0):
    """Terminate a process and any descendants, escalating to SIGKILL.

    Firecracker may be started through wrappers such as sudo/ip-netns/nsenter.
    Killing only the wrapper can leave the real firecracker process orphaned,
    so stop paths should tear down the whole tree rooted at the recorded PID.
    """
    import signal

    if not pid or pid == os.getpid():
        return

    targets = list(reversed(_descendant_pids(pid))) + [pid]
    for target in targets:
        try:
            os.kill(target, signal.SIGTERM)
        except ProcessLookupError:
            pass
        except PermissionError:
            logger.error(f"Permission denied sending SIGTERM to PID {target}")

    deadline = time.time() + timeout
    while time.time() < deadline:
        if not any(_pid_exists(target) for target in targets):
            return
        time.sleep(0.05)

    targets = list(reversed(_descendant_pids(pid))) + [pid]
    for target in targets:
        try:
            os.kill(target, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except PermissionError:
            logger.error(f"Permission denied sending SIGKILL to PID {target}")


FIRECRACKER_BIN = os.environ.get("BANDSOX_FIRECRACKER_BIN", "/usr/bin/firecracker")
DEFAULT_KERNEL_PATH = "/var/lib/bandsox/vmlinux"
# quiet + loglevel=1 silences the ~190-line kernel printk stream that otherwise
# trickles over the emulated serial UART (the host reads it byte-by-byte, adding
# ~45ms of pure transmission time to every boot). The agent still uses
# console=ttyS0 for its own I/O; only kernel chatter is suppressed.
#
# quiet also hides kernel panic/oops detail on boot failures. Set
# BANDSOX_VERBOSE_BOOT=1 to drop it and get full kernel logs when debugging a
# boot regression (costs the ~45ms back).
_BASE_BOOT_ARGS = "console=ttyS0 reboot=k panic=1 pci=off random.trust_cpu=on i8042.noaux i8042.nomux i8042.nopnp i8042.dumbkbd"
DEFAULT_BOOT_ARGS = _BASE_BOOT_ARGS if os.environ.get("BANDSOX_VERBOSE_BOOT") else f"{_BASE_BOOT_ARGS} quiet loglevel=1"
