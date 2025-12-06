import sys
import time
import logging
from bandsox.core import BandSox

# Configure logging
logging.basicConfig(level=logging.INFO, filename='verify_network.log', filemode='w')
logger = logging.getLogger("verify_network")

def main():
    import os
    cwd = os.getcwd()
    # Use absolute path for storage to match server if needed, or local
    # Using local for verification script isolation
    bs = BandSox(storage_dir=f"/home/rc/bandsox/storage")
    
    logger.info("Creating VM...")
    # Use python:alpine as it has ping and wget/curl usually
    vm = bs.create_vm("python:alpine", name="net-test-vm", vcpu=1, mem_mib=256, kernel_path=f"{cwd}/vmlinux", enable_networking=True)
    
    try:
        logger.info(f"VM {vm.vm_id} started. Waiting for boot...")
        time.sleep(10)
        
        def print_output(data):
            logger.info(f"VM: {data.strip()}")
            
        # 1. Check IP configuration
        logger.info("Checking IP configuration...")
        vm.exec_command("ip addr", on_stdout=print_output, on_stderr=print_output)
        vm.exec_command("ip route", on_stdout=print_output, on_stderr=print_output)
        
        # Debug Host TAP
        import subprocess
        logger.info(f"Host TAP status for {vm.tap_name}:")
        subprocess.run(["ip", "addr", "show", vm.tap_name])
        subprocess.run(["ip", "link", "show", vm.tap_name])
        
        # 2. Ping Gateway
        logger.info("Pinging Gateway...")
        # Gateway is usually .1
        # We need to know the gateway IP. 
        # Based on vm.py: host_ip = f"172.16.{subnet_idx}.1"
        # subnet_idx = int(vm.vm_id[-2:], 16)
        subnet_idx = int(vm.vm_id[-2:], 16)
        gateway_ip = f"172.16.{subnet_idx}.1"
        
        exit_code = vm.exec_command(f"ping -c 3 {gateway_ip}", on_stdout=print_output, on_stderr=print_output)
        if exit_code != 0:
            logger.error("Failed to ping gateway")
        else:
            logger.info("Gateway ping SUCCESS")
            
        # 3. Ping Google DNS
        logger.info("Pinging 8.8.8.8...")
        exit_code = vm.exec_command("ping -c 3 8.8.8.8", on_stdout=print_output, on_stderr=print_output)
        if exit_code != 0:
            logger.error("Failed to ping 8.8.8.8")
        else:
            logger.info("Internet ping SUCCESS")
            
        # 4. DNS Resolution
        logger.info("Testing DNS resolution...")
        exit_code = vm.exec_command("nslookup google.com", on_stdout=print_output, on_stderr=print_output)
        if exit_code != 0:
            logger.error("DNS resolution failed")
        else:
            logger.info("DNS resolution SUCCESS")
            
    except Exception as e:
        logger.error(f"Verification failed: {e}")
    finally:
        vm.stop()
        vm.delete()

if __name__ == "__main__":
    main()
