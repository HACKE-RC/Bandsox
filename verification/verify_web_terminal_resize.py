import asyncio
import websockets
import json
import base64
import sys
import time
import requests
import subprocess
import os

# Configuration
HOST = "127.0.0.1"
PORT = 8002
API_URL = f"http://{HOST}:{PORT}"
WS_URL = f"ws://{HOST}:{PORT}"

def start_server():
    env = os.environ.copy()
    proc = subprocess.Popen(
        [sys.executable, "-m", "bandsox.cli", "serve", "--port", str(PORT)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env
    )
    time.sleep(2)
    return proc

async def test_resize(vm_id):
    uri = f"{WS_URL}/api/vms/{vm_id}/terminal?cols=80&rows=24"
    print(f"Connecting to {uri}")
    async with websockets.connect(uri) as websocket:
        # 1. Check initial size
        cmd = "stty size\n"
        encoded = base64.b64encode(cmd.encode()).decode()
        await websocket.send(json.dumps({"type": "input", "data": encoded}))
        
        # Read response
        initial_size_verified = False
        try:
            start_time = time.time()
            while time.time() - start_time < 5:
                try:
                    response = await asyncio.wait_for(websocket.recv(), timeout=1.0)
                    decoded = base64.b64decode(response).decode(errors='ignore')
                    print(f"Received: {decoded!r}")
                    if "24 80" in decoded:
                        print("SUCCESS: Initial size verified")
                        initial_size_verified = True
                        break
                except asyncio.TimeoutError:
                    continue
        except Exception as e:
            print(f"Error reading: {e}")
            
        if not initial_size_verified:
            print("FAILED: Initial size not verified")
            return False

        # 2. Send resize event
        print("Sending resize to 100x40...")
        await websocket.send(json.dumps({
            "type": "resize",
            "cols": 100,
            "rows": 40
        }))
        
        # 3. Check new size
        cmd = "stty size\n"
        encoded = base64.b64encode(cmd.encode()).decode()
        await websocket.send(json.dumps({"type": "input", "data": encoded}))
        
        resized_verified = False
        try:
            start_time = time.time()
            while time.time() - start_time < 5:
                try:
                    response = await asyncio.wait_for(websocket.recv(), timeout=1.0)
                    decoded = base64.b64decode(response).decode(errors='ignore')
                    print(f"Received: {decoded!r}")
                    if "40 100" in decoded:
                        print("SUCCESS: Resize verified")
                        resized_verified = True
                        break
                except asyncio.TimeoutError:
                    continue
        except Exception as e:
            print(f"Error reading: {e}")
            
        if not resized_verified:
            print("FAILED: Resize not verified")
            return False
            
        return True

def main():
    print("Creating VM via API...")
    try:
        server_proc = start_server()
        time.sleep(5)
        
        try:
            requests.get(f"{API_URL}/")
        except:
            print("Server not up")
            server_proc.terminate()
            return

        resp = requests.post(f"{API_URL}/api/vms", json={
            "image": "python:alpine",
            "name": "test-resize-vm",
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
        print(f"Testing resize for VM {vm_id}...")
        success = asyncio.run(test_resize(vm_id))
        
        if success:
            print("Verification PASSED")
        else:
            print("Verification FAILED")
            
    finally:
        if 'vm_id' in locals():
            try:
                requests.delete(f"{API_URL}/api/vms/{vm_id}")
            except:
                pass
        
        if 'server_proc' in locals():
            server_proc.terminate()
            server_proc.wait()

if __name__ == "__main__":
    main()
