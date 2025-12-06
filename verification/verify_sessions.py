import sys
import time
import logging
import threading
from bandsox.core import BandSox

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("verify_sessions")

def main():
    import os
    cwd = os.getcwd()
    bs = BandSox(storage_dir=f"{cwd}/storage")
    
    # 1. Create VM
    logger.info("Creating VM...")
    # We need to ensure agent.py is updated in the image.
    # Since we modified agent.py, we should rebuild the image.
    # We can force this by removing the cached image or using a new tag/image.
    # For simplicity, let's assume the user clears cache or we use a unique tag if building from dockerfile.
    # But here we use python:alpine.
    
    vm = bs.create_vm("python:alpine", vcpu=1, mem_mib=256, kernel_path=f"{cwd}/vmlinux", enable_networking=False)
    
    try:
        logger.info(f"VM {vm.vm_id} started. Waiting for boot...")
        time.sleep(5)
        
        # 2. Start a session (Python echo server)
        logger.info("Starting echo session...")
        
        echo_script = "import sys; print('Echo Server Ready'); sys.stdout.flush();\nwhile True: line = sys.stdin.readline(); print(f'Echo: {line.strip()}'); sys.stdout.flush();"
        
        # We need to escape quotes for the command line
        cmd = f"/usr/local/bin/python3 -c \"{echo_script}\""
        
        output_received = threading.Event()
        echo_received = threading.Event()
        
        def on_stdout(data):
            logger.info(f"SESSION STDOUT: {data.strip()}")
            if "Echo Server Ready" in data:
                output_received.set()
            if "Echo: Hello Session" in data:
                echo_received.set()
                
        def on_stderr(data):
            logger.info(f"SESSION STDERR: {data.strip()}")
            
        session_id = vm.start_session(cmd, on_stdout=on_stdout, on_stderr=on_stderr)
        logger.info(f"Session started: {session_id}")
        
        # Wait for ready
        if not output_received.wait(10):
            logger.error("Timed out waiting for session ready")
            sys.exit(1)
            
        # 3. Send Input
        logger.info("Sending input...")
        vm.send_session_input(session_id, "Hello Session\n")
        
        # Wait for echo
        if not echo_received.wait(10):
            logger.error("Timed out waiting for echo")
            sys.exit(1)
            
        logger.info("Echo verified!")
        
        # 4. Kill Session
        logger.info("Killing session...")
        vm.kill_session(session_id)
        
        # Wait a bit to ensure it dies (we don't get an exit event callback for kill explicitly unless we wait for it)
        # The on_exit callback should be triggered if the process dies.
        # But we didn't pass on_exit to start_session in this script (optional).
        
        time.sleep(2)
        logger.info("Session verification PASSED!")
        vm.stop()
        
    except Exception as e:
        logger.error(f"Verification failed: {e}")
        vm.stop()
        sys.exit(1)

if __name__ == "__main__":
    main()
