import sys
import debugpy
import runpy
import socket
import urllib.request
import threading
import time
import os

def get_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(('10.255.255.255', 1))
        IP = s.getsockname()[0]
    except Exception:
        IP = '127.0.0.1'
    finally:
        s.close()
    return IP

def get_free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(('', 0))
        return s.getsockname()[1]

def register_to_gateway(host, port, target):
    time.sleep(1)
    try:
        gateway_host = os.environ.get("DAP_GATEWAY_HOST", "127.0.0.1")
        url = f"http://{gateway_host}:5680/attach?host={host}&port={port}"
        req = urllib.request.Request(url, method="POST")
        urllib.request.urlopen(req)
        print(f"[DAP Launcher] ✅ Successfully registered to Gateway (Target: {host}:{port})")
    except Exception as e:
        print(f"[DAP Launcher] ⚠️ Warning: Failed to connect to Gateway. - {e}")

def launch():
    if len(sys.argv) < 2:
        print("Usage: python3 dap_launcher.py <target_script.py>")
        sys.exit(1)
        
    target = sys.argv[1]
    port = get_free_port()
    
    host = os.environ.get("DAP_HOST", get_ip())
    
    print("==================================================")
    print(f" [DAP Launcher] 🚀 Launching target: {target}")
    print(f" [DAP Launcher] 💉 Listening on 0.0.0.0:{port}")
    print(f" [DAP Launcher] 🔗 Registered at: {host}:{port}")
    print("==================================================")
    
    debugpy.listen(("0.0.0.0", port))
    threading.Thread(target=register_to_gateway, args=(host, port, target), daemon=True).start()
    
    sys.argv = sys.argv[1:]
    
    class DummyDebugpy:
        def wait_for_client(self): pass
        def breakpoint(self): pass
        
    init_globals = {
        "debugpy": DummyDebugpy()
    }
    
    try:
        runpy.run_path(target, run_name="__main__", init_globals=init_globals)
    except Exception as e:
        print(f"\n[DAP Launcher] ⚠️ Target execution error: {e}")

if __name__ == "__main__":
    launch()
