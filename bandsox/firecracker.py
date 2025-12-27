import requests
import requests_unixsocket
import logging
import json
import time
import os

logger = logging.getLogger(__name__)

# Monkey patch requests to support unix sockets
requests_unixsocket.monkeypatch()

class FirecrackerClient:
    def __init__(self, socket_path: str):
        self.socket_path = socket_path
        # requests_unixsocket requires URL encoded socket path
        # e.g. http+unix://%2Ftmp%2Ffirecracker.socket/
        encoded_path = socket_path.replace("/", "%2F")
        self.base_url = f"http+unix://{encoded_path}"
        
    def _request(self, method, endpoint, data=None, log_error=True):
        url = f"{self.base_url}{endpoint}"
        headers = {'Accept': 'application/json', 'Content-Type': 'application/json'}
        
        logger.debug(f"Firecracker API {method} {endpoint}")
        try:
            if data:
                response = requests.request(method, url, headers=headers, json=data)
            else:
                response = requests.request(method, url, headers=headers)
            
            # Firecracker returns 204 No Content for success often
            if response.status_code not in [200, 204]:
                # Some callers (e.g. snapshot load) intentionally retry after inspecting the error.
                # Allow suppressing loud logs while still surfacing the exception.
                log_fn = logger.error if log_error else logger.debug
                log_fn(f"Firecracker API error {response.status_code}: {response.text}")
                raise Exception(f"Firecracker API error: {response.text}")
            
            return response
        except requests.exceptions.ConnectionError:
            # This happens if Firecracker isn't running yet or socket isn't ready
            raise

    def wait_for_socket(self, timeout=20):
        start = time.time()
        while time.time() - start < timeout:
            if os.path.exists(self.socket_path):
                return True
            time.sleep(0.1)
        return False

    def put_boot_source(self, kernel_image_path: str, boot_args: str):
        data = {
            "kernel_image_path": kernel_image_path,
            "boot_args": boot_args
        }
        return self._request("PUT", "/boot-source", data)

    def put_drives(self, drive_id: str, path_on_host: str, is_root_device: bool = False, is_read_only: bool = False):
        data = {
            "drive_id": drive_id,
            "path_on_host": path_on_host,
            "is_root_device": is_root_device,
            "is_read_only": is_read_only
        }
        return self._request("PUT", f"/drives/{drive_id}", data)

    def patch_drive(self, drive_id: str, path_on_host: str):
        data = {
            "drive_id": drive_id,
            "path_on_host": path_on_host
        }
        return self._request("PATCH", f"/drives/{drive_id}", data)

    def put_network_interface(self, iface_id: str, host_dev_name: str, guest_mac: str = None):
        data = {
            "iface_id": iface_id,
            "host_dev_name": host_dev_name
        }
        if guest_mac:
            data["guest_mac"] = guest_mac
            
        return self._request("PUT", f"/network-interfaces/{iface_id}", data)

    def patch_network_interface(self, iface_id: str, host_dev_name: str):
        data = {
            "iface_id": iface_id,
            "host_dev_name": host_dev_name
        }
        return self._request("PATCH", f"/network-interfaces/{iface_id}", data)

    def put_machine_config(self, vcpu_count: int, mem_size_mib: int):
        data = {
            "vcpu_count": vcpu_count,
            "mem_size_mib": mem_size_mib
        }
        return self._request("PUT", "/machine-config", data)

    def instance_start(self):
        data = {
            "action_type": "InstanceStart"
        }
        return self._request("PUT", "/actions", data)

    def create_snapshot(self, snapshot_path: str, mem_file_path: str):
        data = {
            "snapshot_type": "Full",
            "snapshot_path": snapshot_path,
            "mem_file_path": mem_file_path
        }
        return self._request("PUT", "/snapshot/create", data)

    def load_snapshot(self, snapshot_path: str, mem_file_path: str):
        data = {
            "snapshot_path": snapshot_path,
            "mem_file_path": mem_file_path,
            "enable_diff_snapshots": False,
            "resume_vm": False 
        }
        # Allow the caller to inspect/handle 4xx responses (e.g. missing backing file) without loud logs.
        return self._request("PUT", "/snapshot/load", data, log_error=False)
        
    def resume_vm(self):
        data = {
            "state": "Resumed"
        }
        return self._request("PATCH", "/vm", data)

    def pause_vm(self):
        data = {
            "state": "Paused"
        }
        return self._request("PATCH", "/vm", data)
