import http.client
import socket
import logging
import json
import time
import os

logger = logging.getLogger(__name__)


class _UnixHTTPConnection(http.client.HTTPConnection):
    """http.client connection that talks to a unix domain socket.

    The Firecracker control plane is a plain HTTP/1.1 REST API exposed over
    an AF_UNIX socket, so the stdlib client covers it without pulling in
    requests + urllib3 (~77ms of import time on the VM-boot hot path).
    """

    def __init__(self, socket_path: str, timeout=None):
        super().__init__("localhost", timeout=timeout)
        self._socket_path = socket_path

    def connect(self):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        if self.timeout is not None:
            sock.settimeout(self.timeout)
        sock.connect(self._socket_path)
        self.sock = sock


class _Response:
    """Minimal stand-in for the bits of a requests.Response callers use."""

    def __init__(self, status_code: int, body: bytes):
        self.status_code = status_code
        self._body = body

    @property
    def text(self) -> str:
        return self._body.decode("utf-8", "replace")

    def json(self):
        return json.loads(self._body or b"null")


class FirecrackerClient:
    def __init__(self, socket_path: str):
        self.socket_path = socket_path

    def _request(self, method, endpoint, data=None, log_error=True, timeout=None):
        # timeout defaults to None (block) to match the prior requests-based
        # behaviour: snapshot create/load are synchronous and can run long for
        # large guests, so a blanket short timeout would truncate them. Callers
        # on the fast config path may pass one for hang-safety.
        headers = {"Accept": "application/json"}
        body = None
        if data is not None:
            body = json.dumps(data).encode("utf-8")
            headers["Content-Type"] = "application/json"

        logger.debug(f"Firecracker API {method} {endpoint}")
        # ConnectionRefused/FileNotFound here means Firecracker isn't up yet;
        # let it propagate so callers can retry, matching the old behaviour.
        conn = _UnixHTTPConnection(self.socket_path, timeout=timeout)
        try:
            conn.request(method, endpoint, body=body, headers=headers)
            raw = conn.getresponse()
            response = _Response(raw.status, raw.read())
        finally:
            conn.close()

        # Firecracker returns 204 No Content for success often
        if response.status_code not in [200, 204]:
            # Some callers (e.g. snapshot load) intentionally retry after inspecting the error.
            # Allow suppressing loud logs while still surfacing the exception.
            log_fn = logger.error if log_error else logger.debug
            log_fn(f"Firecracker API error {response.status_code}: {response.text}")
            raise Exception(f"Firecracker API error: {response.text}")

        return response

    def wait_for_socket(self, timeout=20):
        # Firecracker creates the API socket within ~10-15ms. Poll tightly so
        # we don't burn ~90ms sleeping past its arrival on the VM-boot hot path.
        start = time.time()
        while time.time() - start < timeout:
            if os.path.exists(self.socket_path):
                return True
            time.sleep(0.001)
        return False

    def put_boot_source(self, kernel_image_path: str, boot_args: str):
        data = {"kernel_image_path": kernel_image_path, "boot_args": boot_args}
        return self._request("PUT", "/boot-source", data)

    def put_drives(
        self,
        drive_id: str,
        path_on_host: str,
        is_root_device: bool = False,
        is_read_only: bool = False,
        rate_limit_bandwidth_mbps: int = 0,
        rate_limit_iops: int = 0,
        io_engine: str = "Async",
    ):
        data = {
            "drive_id": drive_id,
            "path_on_host": path_on_host,
            "is_root_device": is_root_device,
            "is_read_only": is_read_only,
            "io_engine": io_engine,
        }

        if rate_limit_bandwidth_mbps > 0 or rate_limit_iops > 0:
            rate_limiter = {}
            if rate_limit_bandwidth_mbps > 0:
                rate_limiter["bandwidth"] = {
                    "size": rate_limit_bandwidth_mbps * 1024 * 1024,
                    "one_time_burst": rate_limit_bandwidth_mbps * 1024 * 1024,
                    "refill_time": 1000,
                }
            if rate_limit_iops > 0:
                rate_limiter["ops"] = {
                    "size": rate_limit_iops,
                    "one_time_burst": rate_limit_iops,
                    "refill_time": 1000,
                }
            data["rate_limiter"] = rate_limiter

        return self._request("PUT", f"/drives/{drive_id}", data)

    def patch_drive(self, drive_id: str, path_on_host: str):
        data = {"drive_id": drive_id, "path_on_host": path_on_host}
        return self._request("PATCH", f"/drives/{drive_id}", data)

    def put_network_interface(
        self, iface_id: str, host_dev_name: str, guest_mac: str = None
    ):
        data = {"iface_id": iface_id, "host_dev_name": host_dev_name}
        if guest_mac:
            data["guest_mac"] = guest_mac

        return self._request("PUT", f"/network-interfaces/{iface_id}", data)

    def patch_network_interface(self, iface_id: str, host_dev_name: str):
        data = {"iface_id": iface_id, "host_dev_name": host_dev_name}
        return self._request("PATCH", f"/network-interfaces/{iface_id}", data)

    def put_machine_config(self, vcpu_count: int, mem_size_mib: int):
        data = {"vcpu_count": vcpu_count, "mem_size_mib": mem_size_mib}
        return self._request("PUT", "/machine-config", data)

    def put_entropy(
        self,
        bandwidth_size: int = 1048576,
        refill_time_ms: int = 1000,
        one_time_burst: int = 0,
    ):
        """Configures the virtio-rng entropy device.

        Must be called pre-boot. Firecracker rejects this after the VM
        starts, and snapshot-restore paths can't call it before load.
        """
        data = {
            "rate_limiter": {
                "bandwidth": {
                    "size": bandwidth_size,
                    "one_time_burst": one_time_burst,
                    "refill_time": refill_time_ms,
                }
            }
        }
        return self._request("PUT", "/entropy", data)

    def instance_start(self):
        data = {"action_type": "InstanceStart"}
        return self._request("PUT", "/actions", data)

    def create_snapshot(self, snapshot_path: str, mem_file_path: str):
        data = {
            "snapshot_type": "Full",
            "snapshot_path": snapshot_path,
            "mem_file_path": mem_file_path,
        }
        return self._request("PUT", "/snapshot/create", data)

    def load_snapshot(self, snapshot_path: str, mem_file_path: str):
        data = {
            "snapshot_path": snapshot_path,
            "mem_file_path": mem_file_path,
            "enable_diff_snapshots": False,
            "resume_vm": False,
        }
        # Allow the caller to inspect/handle 4xx responses (e.g. missing backing file) without loud logs.
        return self._request("PUT", "/snapshot/load", data, log_error=False)

    def resume_vm(self):
        data = {"state": "Resumed"}
        return self._request("PATCH", "/vm", data)

    def pause_vm(self):
        data = {"state": "Paused"}
        return self._request("PATCH", "/vm", data)

    def put_vsock(self, vsock_id: str, guest_cid: int, uds_path: str):
        """
        Configures a vsock device for the VM.

        Args:
            vsock_id: Identifier for the vsock device (e.g., "vsock0")
            guest_cid: Context ID for the guest VM (must be >= 3)
            uds_path: Unix domain socket path on host (e.g., "/tmp/bandsox/vsock_abc123.sock")

        Firecracker API: PUT /vsock
        {
            "vsock_id": "vsock0",
            "guest_cid": 3,
            "uds_path": "/path/to/v.sock"
        }
        """
        data = {"vsock_id": vsock_id, "guest_cid": guest_cid, "uds_path": uds_path}
        return self._request("PUT", f"/vsock/{vsock_id}", data)
