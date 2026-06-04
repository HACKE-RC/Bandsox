import os
import subprocess
import sys


def test_firecracker_bin_can_be_overridden_for_local_testing():
    env = dict(os.environ)
    env["BANDSOX_FIRECRACKER_BIN"] = "/tmp/bandsox-test-firecracker"
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from bandsox.vm_common import FIRECRACKER_BIN; print(FIRECRACKER_BIN)",
        ],
        env=env,
        check=True,
        text=True,
        capture_output=True,
    )
    assert result.stdout.strip() == "/tmp/bandsox-test-firecracker"
