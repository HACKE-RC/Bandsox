import asyncio
import websockets
import json
import base64
import sys
import time
import requests
import subprocess
import os
from bandsox.core import BandSox

# Configuration
HOST = "127.0.0.1"
PORT = 8001
API_URL = f"http://{HOST}:{PORT}"
WS_URL = f"ws://{HOST}:{PORT}"

def start_server():
    # Start server in background
    env = os.environ.copy()
    proc = subprocess.Popen(
        [sys.executable, "-m", "bandsox.cli", "serve", "--port", str(PORT)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env
    )
    time.sleep(2) # Wait for server to start
    return proc

async def test_terminal(vm_id):
    uri = f"{WS_URL}/api/vms/{vm_id}/terminal?cols=80&rows=24"
    print(f"Connecting to {uri}")
    async with websockets.connect(uri) as websocket:
        # Send a command
        cmd = "echo Hello World\n"
        encoded = base64.b64encode(cmd.encode()).decode()
        await websocket.send(json.dumps({"type": "input", "data": encoded}))
        
        # Read response
        try:
            while True:
                response = await websocket.recv()
                # response is base64 encoded
                decoded = base64.b64decode(response).decode(errors='ignore')
                print(f"Received: {decoded!r}")
                if "Hello World" in decoded:
                    print("SUCCESS: Received expected output")
                    return True
        except Exception as e:
            print(f"Error reading: {e}")
            return False

def main():
    print("Creating VM via API...")
    try:
        # Wait for server to be up first
        server_proc = start_server()
        time.sleep(5)
        
        # Check server
        try:
            requests.get(f"{API_URL}/")
        except:
            print("Server not up")
            server_proc.terminate()
            return

        # Create VM
        resp = requests.post(f"{API_URL}/api/vms", json={
            "image": "python:alpine",
            "name": "test-term-vm",
            "vcpu": 1,
            "mem_mib": 256,
            "enable_networking": False
        })
        if resp.status_code != 200:
            print(f"Failed to create VM: {resp.text}")
            server_proc.terminate()
            return
            
        vm_id = resp.json()["id"]
        print(f"VM {vm_id} created via API.")
        
    except Exception as e:
        print(f"Failed to create VM: {e}")
        if 'server_proc' in locals():
            server_proc.terminate()
        return

    print(f"VM {vm_id} started. Waiting for boot...")
    time.sleep(10)

    try:
        print(f"Testing terminal for VM {vm_id}...")
        success = asyncio.run(test_terminal(vm_id))
        
        if success:
            print("Verification PASSED")
        else:
            print("Verification FAILED")
            
    finally:
        # Cleanup via API
        if 'vm_id' in locals():
            try:
                requests.delete(f"{API_URL}/api/vms/{vm_id}")
            except:
                pass
        
        if 'server_proc' in locals():
            if server_proc.poll() is not None:
                print(f"Server crashed with code {server_proc.returncode}")
                out, err = server_proc.communicate()
                print(f"Server stdout: {out.decode()}")
                print(f"Server stderr: {err.decode()}")
            else:
                server_proc.terminate()
                server_proc.wait()

if __name__ == "__main__":
    main()
