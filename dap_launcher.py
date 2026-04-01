import sys
import debugpy
import runpy
import socket
import urllib.request
import threading
import time

def get_free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(('', 0))
        return s.getsockname()[1]

def register_to_gateway(port, target):
    time.sleep(1)
    try:
        req = urllib.request.Request(f"http://127.0.0.1:5680/attach?port={port}", method="POST")
        urllib.request.urlopen(req)
        print(f"[DAP Launcher] ✅ Successfully registered to Gateway (Port: {port})")
    except Exception as e:
        print(f"[DAP Launcher] ⚠️ Warning: Failed to connect to Gateway. Is port 5680 open? - {e}")

def launch():
    if len(sys.argv) < 2:
        print("Usage: python3 dap_launcher.py <target_script.py>")
        sys.exit(1)
        
    target = sys.argv[1]
    port = get_free_port()
    
    print("==================================================")
    print(f" [DAP Launcher] 🚀 Launching target: {target}")
    print(f" [DAP Launcher] 💉 Injecting DAP Probe (Port: {port})...")
    print("==================================================")
    
    debugpy.listen(("127.0.0.1", port))
    threading.Thread(target=register_to_gateway, args=(port, target), daemon=True).start()
    
    sys.argv = sys.argv[1:]
    
    # Monkey Patching to bypass any residual debugpy calls in target code
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
