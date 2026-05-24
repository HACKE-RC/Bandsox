"""
Verify the Go guest agent works identically to the Python agent.

Tests every protocol command inside a real Firecracker microVM.
Requires: sudo, firecracker at /usr/bin/firecracker, kernel at /var/lib/bandsox/vmlinux
"""

import os
import sys
import time
import shutil
import base64
import hashlib
import logging
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger("verify_go_agent")

PASS = 0
FAIL = 0


def check(name, condition, detail=""):
    global PASS, FAIL
    if condition:
        PASS += 1
        logger.info(f"  PASS: {name}")
    else:
        FAIL += 1
        logger.error(f"  FAIL: {name} {detail}")


def main():
    global PASS, FAIL

    cwd = os.getcwd()
    storage_dir = f"{cwd}/test_storage_go_agent"
    if os.path.exists(storage_dir):
        shutil.rmtree(storage_dir)

    from bandsox.core import BandSox

    bs = BandSox(storage_dir=storage_dir)

    logger.info("=== Test 1: Create VM with Go agent ===")
    try:
        vm = bs.create_vm(
            "python:alpine",
            vcpu=1,
            mem_mib=256,
            kernel_path="/var/lib/bandsox/vmlinux",
            enable_networking=False,
        )
    except Exception as e:
        logger.error(f"Failed to create VM: {e}")
        sys.exit(1)

    try:
        logger.info(f"VM {vm.vm_id} started, waiting for agent...")
        time.sleep(3)
        check("agent_ready", vm.agent_ready, f"agent_ready={vm.agent_ready}")
        if not vm.agent_ready:
            logger.error("Agent not ready — cannot continue")
            vm.stop()
            sys.exit(1)

        # =====================================================================
        logger.info("\n=== Test 2: Basic shell exec ===")
        # =====================================================================

        exit_code = vm.exec_command("echo hello")
        check("echo hello exit_code=0", exit_code == 0, f"got {exit_code}")

        exit_code = vm.exec_command("false")
        check("false exit_code=1", exit_code == 1, f"got {exit_code}")

        exit_code = vm.exec_command("exit 42")
        check("exit 42 exit_code=42", exit_code == 42, f"got {exit_code}")

        # =====================================================================
        logger.info("\n=== Test 3: Command output capture ===")
        # =====================================================================

        stdout_lines = []

        def on_stdout(line):
            stdout_lines.append(line)

        exit_code = vm.exec_command("echo -n hello_world", on_stdout=on_stdout)
        check("echo capture exit=0", exit_code == 0, f"got {exit_code}")
        check("echo capture output", "hello_world" in "".join(stdout_lines),
              f"got: {''.join(stdout_lines)!r}")

        # =====================================================================
        logger.info("\n=== Test 4: stderr capture ===")
        # =====================================================================

        stderr_lines = []

        def on_stderr(line):
            stderr_lines.append(line)

        exit_code = vm.exec_command(
            "echo err_msg >&2", on_stdout=on_stdout, on_stderr=on_stderr
        )
        check("stderr capture exit=0", exit_code == 0, f"got {exit_code}")
        check("stderr has message", any("err_msg" in l for l in stderr_lines),
              f"got: {stderr_lines}")

        # =====================================================================
        logger.info("\n=== Test 5: write_file ===")
        # =====================================================================

        test_content = "Hello, BandSox Go Agent!\nLine 2\nLine 3\n"
        local_src = "test_go_write.txt"
        with open(local_src, "w") as f:
            f.write(test_content)

        vm.upload_file(local_src, "/tmp/go_write_test.txt")

        exit_code = vm.exec_command("test -f /tmp/go_write_test.txt")
        check("file exists after write", exit_code == 0, f"got {exit_code}")

        # =====================================================================
        logger.info("\n=== Test 6: get_file_contents ===")
        # =====================================================================

        content = vm.get_file_contents("/tmp/go_write_test.txt")
        check("file content matches", content == test_content,
              f"expected {test_content!r}, got {content!r}")

        # =====================================================================
        logger.info("\n=== Test 7: get_file_contents with offset/limit ===")
        # =====================================================================

        trimmed = vm.get_file_contents("/tmp/go_write_test.txt", offset=1, limit=1)
        check("offset=1 limit=1 returns line 2",
              "Line 2" in trimmed and "Hello" not in trimmed,
              f"got: {trimmed!r}")

        # =====================================================================
        logger.info("\n=== Test 8: get_file_contents with line numbers ===")
        # =====================================================================

        numbered = vm.get_file_contents(
            "/tmp/go_write_test.txt", show_line_numbers=True
        )
        check("line numbers present", "1\t" in numbered, f"got: {numbered!r}")
        check("line 2 numbered", "2\t" in numbered, f"got: {numbered!r}")

        # =====================================================================
        logger.info("\n=== Test 9: get_file_contents header/footer ===")
        # =====================================================================

        partial = vm.get_file_contents(
            "/tmp/go_write_test.txt", offset=1, limit=1, show_header=True, show_footer=True
        )
        check("header shows skipped",
              "skipped 1 lines" in partial, f"got: {partial!r}")
        check("footer shows remaining",
              "lines left" in partial, f"got: {partial!r}")

        # =====================================================================
        logger.info("\n=== Test 10: download_file ===")
        # =====================================================================

        download_path = "test_go_download.txt"
        if os.path.exists(download_path):
            os.unlink(download_path)

        vm.download_file("/tmp/go_write_test.txt", download_path)
        with open(download_path, "r") as f:
            downloaded = f.read()
        check("download matches source", downloaded == test_content,
              f"expected {test_content!r}, got {downloaded!r}")

        # =====================================================================
        logger.info("\n=== Test 11: Binary file roundtrip ===")
        # =====================================================================

        binary_size = 64 * 1024  # 64KB
        binary_data = os.urandom(binary_size)
        with open("test_go_binary.bin", "wb") as f:
            f.write(binary_data)

        vm.upload_file("test_go_binary.bin", "/tmp/go_binary.bin")
        vm.download_file("/tmp/go_binary.bin", "test_go_binary_out.bin")

        with open("test_go_binary_out.bin", "rb") as f:
            roundtripped = f.read()

        check("binary roundtrip size match", len(roundtripped) == binary_size,
              f"expected {binary_size}, got {len(roundtripped)}")
        check("binary roundtrip hash match",
              hashlib.md5(binary_data).hexdigest() == hashlib.md5(roundtripped).hexdigest())

        # =====================================================================
        logger.info("\n=== Test 12: Append write ===")
        # =====================================================================

        append_content = "\nAppended line\n"
        with open("test_go_append.txt", "w") as f:
            f.write(append_content)

        vm.upload_file("test_go_append.txt", "/tmp/go_write_test.txt", append=True)
        final_content = vm.get_file_contents("/tmp/go_write_test.txt")
        check("append preserved original",
              test_content in final_content,
              f"got: {final_content!r}")
        check("append added new content",
              "Appended line" in final_content,
              f"got: {final_content!r}")

        # =====================================================================
        logger.info("\n=== Test 13: list_dir ===")
        # =====================================================================

        vm.exec_command("mkdir -p /tmp/go_listdir/a /tmp/go_listdir/b")
        vm.exec_command("touch /tmp/go_listdir/x.txt /tmp/go_listdir/y.py")

        files = vm.list_dir("/tmp/go_listdir")
        names = {f["name"] for f in files}
        check("dirs listed", "a" in names and "b" in names, f"got: {names}")
        check("files listed", "x.txt" in names and "y.py" in names, f"got: {names}")

        # Check file types
        for f in files:
            if f["name"] == "a":
                check("directory type correct", f["type"] == "directory",
                      f"got {f['type']}")

        # =====================================================================
        logger.info("\n=== Test 14: file_info ===")
        # =====================================================================

        info = vm.get_file_info("/tmp/go_write_test.txt")
        check("file_info has size", info.get("size") is not None, f"got: {info}")
        check("file_info has mtime", info.get("mtime") is not None, f"got: {info}")

        # =====================================================================
        logger.info("\n=== Test 15: Large file read/write (tests vsock path) ===")
        # =====================================================================

        large_size = 256 * 1024  # 256KB — triggers vsock on both read and write
        large_data = os.urandom(large_size)
        with open("test_go_large.bin", "wb") as f:
            f.write(large_data)

        start = time.time()
        vm.upload_file("test_go_large.bin", "/tmp/go_large.bin")
        upload_time = time.time() - start

        start = time.time()
        vm.download_file("/tmp/go_large.bin", "test_go_large_out.bin")
        download_time = time.time() - start

        with open("test_go_large_out.bin", "rb") as f:
            large_roundtripped = f.read()

        check("large file size match", len(large_roundtripped) == large_size,
              f"expected {large_size}, got {len(large_roundtripped)}")
        check("large file hash match",
              hashlib.md5(large_data).hexdigest() == hashlib.md5(large_roundtripped).hexdigest())
        check("large upload < 10s", upload_time < 10,
              f"took {upload_time:.2f}s")
        check("large download < 10s", download_time < 10,
              f"took {download_time:.2f}s")

        # =====================================================================
        logger.info("\n=== Test 16: Non-existent file error handling ===")
        # =====================================================================

        try:
            vm.get_file_contents("/tmp/does_not_exist_xyz.txt")
            check("non-existent file raises", False, "should have raised")
        except Exception as e:
            check("non-existent file raises error", True, str(e))

        # =====================================================================
        logger.info("\n=== Test 17: Background session ===")
        # =====================================================================

        session_id, pid = vm.start_session("sleep 10")
        check("background session started", session_id is not None)
        check("background session has PID", pid is not None and pid > 0,
              f"pid={pid}")

        # Check process is running
        exit_code = vm.exec_command(f"kill -0 {pid}")
        check("background process running", exit_code == 0,
              f"kill -0 returned {exit_code}")

        # Kill the session
        vm.kill_session(session_id)
        time.sleep(0.5)

        exit_code = vm.exec_command(f"kill -0 {pid}")
        check("background process killed", exit_code != 0,
              f"process still alive, kill -0 returned {exit_code}")

        # =====================================================================
        logger.info("\n=== Test 18: Environment variables ===")
        # =====================================================================

        vm.env_vars["TEST_VAR"] = "test_value_123"
        stdout_lines.clear()
        vm.exec_command('echo $TEST_VAR', on_stdout=on_stdout)
        check("env var passed to command",
              any("test_value_123" in l for l in stdout_lines),
              f"got: {stdout_lines}")

    except Exception as e:
        logger.error(f"Test failed: {e}")
        import traceback
        traceback.print_exc()
        FAIL += 1
    finally:
        logger.info("\n=== Cleaning up ===")
        try:
            vm.stop()
        except Exception:
            pass
        # Cleanup local files
        for f in [
            "test_go_write.txt", "test_go_download.txt", "test_go_binary.bin",
            "test_go_binary_out.bin", "test_go_append.txt", "test_go_large.bin",
            "test_go_large_out.bin",
        ]:
            if os.path.exists(f):
                os.unlink(f)
        if os.path.exists("test_storage_go_agent"):
            shutil.rmtree("test_storage_go_agent")

    logger.info(f"\n{'='*50}")
    logger.info(f"Results: {PASS} passed, {FAIL} failed out of {PASS + FAIL}")
    logger.info(f"{'='*50}")

    if FAIL > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
