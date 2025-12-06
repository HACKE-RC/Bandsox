import os
import time
import shutil
import logging
from pathlib import Path
from bandsox.core import BandSox

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("verify_file_ops")

def main():
    cwd = os.getcwd()
    # Use a separate storage dir for testing to avoid messing with existing VMs
    storage_dir = f"{cwd}/test_storage_file_ops"
    if os.path.exists(storage_dir):
        shutil.rmtree(storage_dir)
        
    bs = BandSox(storage_dir=storage_dir)
    
    logger.info("Creating VM...")
    # Use python:alpine as it's small and likely available/cached
    vm = bs.create_vm("python:alpine", vcpu=1, mem_mib=256, kernel_path=f"{cwd}/vmlinux", enable_networking=False)
    
    try:
        logger.info(f"VM {vm.vm_id} started.")
        
        # Wait for agent to be ready (create_vm waits for start, but agent might take a moment)
        # create_vm calls vm.start() which calls client.instance_start().
        # vm.start_process() starts the process and read thread.
        # The read thread sets agent_ready when it sees "ready" status.
        # create_vm doesn't explicitly wait for agent_ready, but let's wait a bit to be sure.
        time.sleep(2)
        
        # 1. Test upload_file and get_file_contents
        logger.info("Testing upload_file and get_file_contents...")
        local_file = "test_upload.txt"
        content = "Hello, BandSox File Ops!"
        with open(local_file, "w") as f:
            f.write(content)
            
        remote_path = "/tmp/test_upload.txt"
        vm.upload_file(local_file, remote_path)
        
        read_content = vm.get_file_contents(remote_path)
        logger.info(f"Read content: {read_content}")
        
        if read_content != content:
            logger.error(f"Content mismatch! Expected: {content}, Got: {read_content}")
            exit(1)
        else:
            logger.info("upload_file and get_file_contents passed.")
            
        # 2. Test download_file
        logger.info("Testing download_file...")
        download_path = "test_download.txt"
        if os.path.exists(download_path):
            os.unlink(download_path)
            
        vm.download_file(remote_path, download_path)
        
        with open(download_path, "r") as f:
            downloaded_content = f.read()
            
        if downloaded_content != content:
            logger.error(f"Download content mismatch! Expected: {content}, Got: {downloaded_content}")
            exit(1)
        else:
            logger.info("download_file passed.")
            
        # 3. Test upload_folder
        logger.info("Testing upload_folder...")
        local_folder = "test_folder"
        if os.path.exists(local_folder):
            shutil.rmtree(local_folder)
        os.makedirs(local_folder)
        
        with open(f"{local_folder}/file1.txt", "w") as f:
            f.write("File 1")
        with open(f"{local_folder}/file2.py", "w") as f:
            f.write("print('File 2')")
        os.makedirs(f"{local_folder}/subdir")
        with open(f"{local_folder}/subdir/file3.txt", "w") as f:
            f.write("File 3")
            
        remote_folder = "/tmp/test_folder"
        
        # Test with pattern
        logger.info("Uploading folder with pattern *.txt...")
        vm.upload_folder(local_folder, remote_folder, pattern="*.txt")
        
        # Verify
        # file1.txt and subdir/file3.txt should exist. file2.py should not.
        
        def check_exists(path):
            return vm.exec_command(f"test -f {path}") == 0
            
        if not check_exists(f"{remote_folder}/file1.txt"):
            logger.error("file1.txt missing")
            exit(1)
        if not check_exists(f"{remote_folder}/subdir/file3.txt"):
            logger.error("subdir/file3.txt missing")
            exit(1)
        if check_exists(f"{remote_folder}/file2.py"):
            logger.error("file2.py should not exist (pattern mismatch)")
            exit(1)
            
        logger.info("upload_folder with pattern passed.")
        
        # Test full upload
        logger.info("Uploading full folder...")
        vm.upload_folder(local_folder, remote_folder) # Overwrite/merge
        
        if not check_exists(f"{remote_folder}/file2.py"):
            logger.error("file2.py missing after full upload")
            exit(1)
            
        logger.info("Full upload_folder passed.")
        
        # Test skip_pattern
        logger.info("Testing skip_pattern...")
        remote_folder_skip = "/tmp/test_folder_skip"
        vm.upload_folder(local_folder, remote_folder_skip, skip_pattern=["*.py"])
        
        if not check_exists(f"{remote_folder_skip}/file1.txt"):
             logger.error("file1.txt missing in skip test")
             exit(1)
        if check_exists(f"{remote_folder_skip}/file2.py"):
             logger.error("file2.py should be skipped")
             exit(1)
             
        logger.info("upload_folder with skip_pattern passed.")

    except Exception as e:
        logger.error(f"Test failed: {e}")
        import traceback
        traceback.print_exc()
        exit(1)
    finally:
        logger.info("Stopping VM...")
        vm.stop()
        # Cleanup local files
        if os.path.exists("test_upload.txt"): os.unlink("test_upload.txt")
        if os.path.exists("test_download.txt"): os.unlink("test_download.txt")
        if os.path.exists("test_folder"): shutil.rmtree("test_folder")
        if os.path.exists(storage_dir): shutil.rmtree(storage_dir)

if __name__ == "__main__":
    main()
