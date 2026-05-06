"""Benchmark BandSox Go guest-agent operations in a real Firecracker VM.

Requires sudo/KVM and /var/lib/bandsox/vmlinux. Intended to run as:
    sudo env PATH=$PATH uv run python verification/benchmark_go_agent.py
"""

import hashlib
import os
import shutil
import statistics
import time
from pathlib import Path

from bandsox.core import BandSox


def timed(fn, repeats=1):
    vals = []
    for _ in range(repeats):
        start = time.perf_counter()
        fn()
        vals.append(time.perf_counter() - start)
    return vals


def fmt_rate(size, seconds):
    if seconds <= 0:
        return "inf MiB/s"
    return f"{size / (1024 * 1024) / seconds:.1f} MiB/s"


def main():
    cwd = Path.cwd()
    storage_dir = Path("/tmp/bandsox-bench-go-agent")
    if storage_dir.exists():
        shutil.rmtree(storage_dir)

    bs = BandSox(storage_dir=str(storage_dir))
    vm = bs.create_vm(
        "python:alpine",
        vcpu=1,
        mem_mib=256,
        kernel_path="/var/lib/bandsox/vmlinux",
        enable_networking=False,
    )

    try:
        deadline = time.time() + 20
        while not vm.agent_ready and time.time() < deadline:
            time.sleep(0.1)
        if not vm.agent_ready:
            raise RuntimeError("agent did not become ready")

        print("# exec latency")
        vals = timed(lambda: vm.exec_command("true"), repeats=20)
        print(f"true: mean={statistics.mean(vals)*1000:.2f}ms p95={statistics.quantiles(vals, n=20)[18]*1000:.2f}ms")

        print("\n# file transfer")
        for size in (64 * 1024, 1 * 1024 * 1024, 8 * 1024 * 1024):
            data = os.urandom(size)
            src = cwd / f"bench_{size}.bin"
            dst = cwd / f"bench_{size}.out.bin"
            src.write_bytes(data)
            if dst.exists():
                dst.unlink()

            up = timed(lambda: vm.upload_file(str(src), f"/tmp/bench_{size}.bin"), repeats=1)[0]
            down = timed(lambda: vm.download_file(f"/tmp/bench_{size}.bin", str(dst)), repeats=1)[0]
            out = dst.read_bytes()
            ok = hashlib.md5(out).digest() == hashlib.md5(data).digest()
            print(
                f"{size//1024} KiB: upload={up:.3f}s ({fmt_rate(size, up)}), "
                f"download={down:.3f}s ({fmt_rate(size, down)}), ok={ok}"
            )
            src.unlink(missing_ok=True)
            dst.unlink(missing_ok=True)
    finally:
        try:
            vm.stop()
        finally:
            shutil.rmtree(storage_dir, ignore_errors=True)


if __name__ == "__main__":
    main()
