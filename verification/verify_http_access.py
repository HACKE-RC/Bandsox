
import logging
import time
import os
import subprocess
import sys
from bandsox.core import BandSox

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("verify_http")

def main():
    cwd = os.getcwd()
    bs = BandSox(storage_dir=f"{cwd}/storage")
    
    # Use python:alpine as it definitely has python
    # We assume the image is available or will be pulled/built
    image_tag = "python:alpine" 
    
    logger.info(f"Creating VM from {image_tag}...")
    vm = bs.create_vm(image_tag, name="http-test", vcpu=1, mem_mib=256, kernel_path=f"{cwd}/vmlinux", enable_networking=True)
    
    try:
        logger.info(f"VM {vm.vm_id} created.")
        
        # 1. Wait for agent
        logger.info("Waiting for agent...")
        if not vm.wait_for_agent(timeout=60):
            logger.error("Agent failed to start")
            sys.exit(1)
            
        # 2. Get IP
        guest_ip = vm.get_guest_ip()
        logger.info(f"Guest IP: {guest_ip}")
        if not guest_ip:
            logger.error("Failed to get Guest IP")
            sys.exit(1)

        # 3. Start Python HTTP Server
        logger.info("Starting Python HTTP server inside VM...")
        
        # We start it as a background session
        # Use unbuffered output to see logs immediately
        # Use full path as PATH might be minimal in agent environment
        cmd = "/usr/local/bin/python3 -u -m http.server 8000 --bind 0.0.0.0"
        
        def on_stdout(line):
            logger.info(f"VM_HTTP: {line.strip()}")
            
        def on_stderr(line):
            logger.info(f"VM_HTTP_ERR: {line.strip()}")
            
        session_id = vm.start_session(cmd, on_stdout=on_stdout, on_stderr=on_stderr)
        
        logger.info("Waiting 10s for server to start...")
        time.sleep(10) # Wait for server to bind
        
        # 4. Verify with CURL from Host
        target_url = f"http://{guest_ip}:8000"
        logger.info(f"Attempting to CURL {target_url} from host...")
        
        try:
            # Use curl with timeout and noproxy to avoid proxy issues with local IPs
            result = subprocess.run(
                ["curl", "-v", "--noproxy", "*", "--connect-timeout", "5", target_url], 
                capture_output=True, 
                text=True
            )
            
            logger.info(f"CURL stdout: {result.stdout}")
            logger.info(f"CURL stderr: {result.stderr}")
            
            if result.returncode == 0:
                logger.info("SUCCESS: Curl request successful!")
            else:
                logger.error(f"FAILURE: Curl returned code {result.returncode}")
                # Debugging: Check network inside VM
                logger.info("Debugging network inside VM:")
                vm.exec_command("ps aux", on_stdout=lambda x: logger.info(f"PS: {x.strip()}"))
                vm.exec_command("wget -O- http://127.0.0.1:8000", on_stdout=lambda x: logger.info(f"WGET: {x.strip()}"), on_stderr=lambda x: logger.info(f"WGET_ERR: {x.strip()}"))
                vm.exec_command("nc -zv 127.0.0.1 8000", on_stdout=lambda x: logger.info(f"NC: {x.strip()}"), on_stderr=lambda x: logger.info(f"NC_ERR: {x.strip()}"))
                vm.exec_command("ls -l /proc/$(pgrep python)/fd", on_stdout=lambda x: logger.info(f"FDS: {x.strip()}"))
                vm.exec_command("cat /proc/net/tcp6", on_stdout=lambda x: logger.info(f"TCP6: {x.strip()}"))
                vm.exec_command("ip addr", on_stdout=lambda x: logger.info(f"IP: {x.strip()}"))
                vm.exec_command("ip route", on_stdout=lambda x: logger.info(f"ROUTE: {x.strip()}"))
                vm.exec_command("netstat -tulpn", on_stdout=lambda x: logger.info(f"NETSTAT: {x.strip()}"))
                # Do not exit, continue to verify library function
                # sys.exit(1)
                
        except Exception as e:
            logger.error(f"Error running curl: {e}")
            sys.exit(1)

        # 5. Verify using Library Function
        logger.info("Verifying using vm.send_http_request()...")
        try:
            resp = vm.send_http_request(port=8000, timeout=5)
            logger.info(f"Library Request Status: {resp.status_code}")
            if resp.status_code == 200:
                logger.info("SUCCESS: Library function successful!")
            else:
                logger.error("FAILURE: Library function returned unexpected status")
        except Exception as e:
            logger.error(f"FAILURE: Library function threw exception: {e}")

    finally:
        logger.info("Cleaning up...")
        vm.stop()
        vm.delete()

if __name__ == "__main__":
    main()
