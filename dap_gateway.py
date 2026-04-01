import socket
import json
import time
import sys
import threading
from queue import Queue
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

class AsyncDAPClient:
    """非同步 DAP Client (每個 Target 一個實例)"""
    def __init__(self, port):
        self.port = port
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.connect(("127.0.0.1", port))
        self.seq = 1
        self.responses = {}
        self.events = Queue()
        self.running = True
        self.listener_thread = threading.Thread(target=self._listen_loop, daemon=True)
        self.listener_thread.start()
        self._handshake()

    def send(self, command, args=None):
        req_seq = self.seq
        req = {"seq": req_seq, "type": "request", "command": command}
        if args: req["arguments"] = args
        body = json.dumps(req).encode('utf-8')
        header = f"Content-Length: {len(body)}\r\n\r\n".encode('utf-8')
        self.sock.sendall(header + body)
        self.seq += 1
        return req_seq

    def _listen_loop(self):
        buffer = b""
        while self.running:
            try:
                while b"\r\n\r\n" not in buffer:
                    data = self.sock.recv(4096)
                    if not data: return
                    buffer += data
                
                header_part, buffer = buffer.split(b"\r\n\r\n", 1)
                length = int(header_part.split(b": ")[1])
                
                while len(buffer) < length:
                    buffer += self.sock.recv(4096)
                body = buffer[:length]
                buffer = buffer[length:]
                
                msg = json.loads(body.decode('utf-8'))
                if msg.get("type") == "response":
                    self.responses[msg.get("request_seq")] = msg
                elif msg.get("type") == "event":
                    self.events.put(msg)
            except Exception:
                break

    def wait_for_response(self, req_seq, timeout=5):
        start = time.time()
        while time.time() - start < timeout:
            if req_seq in self.responses:
                return self.responses.pop(req_seq)
            time.sleep(0.01)
        return None

    def wait_for_event(self, event_name, timeout=5):
        start = time.time()
        while time.time() - start < timeout:
            temp_queue = []
            found = None
            while not self.events.empty():
                msg = self.events.get()
                if msg.get("event") == event_name:
                    found = msg
                    break
                else:
                    temp_queue.append(msg)
            for m in temp_queue:
                self.events.put(m)
            if found: return found
            time.sleep(0.01)
        return None

    def _handshake(self):
        seq_init = self.send("initialize", {"clientID": f"agent-{self.port}", "clientName": "DAP-Gateway", "adapterID": "python"})
        self.wait_for_response(seq_init)
        seq_attach = self.send("attach", {"name": f"Target {self.port}", "request": "attach"})
        self.wait_for_event("initialized")
        seq_config = self.send("configurationDone")
        self.wait_for_response(seq_attach)
        self.wait_for_response(seq_config)

    def get_thread_id(self):
        seq_threads = self.send("threads")
        resp = self.wait_for_response(seq_threads)
        if resp and resp["body"]["threads"]:
            return resp["body"]["threads"][0]["id"]
        return None

    def pause(self):
        tid = self.get_thread_id()
        if tid:
            self.send("pause", {"threadId": tid})
            self.wait_for_event("stopped")
            return True
        return False

    def resume(self):
        tid = self.get_thread_id()
        if tid:
            self.send("continue", {"threadId": tid})
            return True
        return False

    def get_snapshot(self):
        tid = self.get_thread_id()
        if not tid: return {"error": "No thread found"}
        
        seq_st = self.send("stackTrace", {"threadId": tid})
        st_resp = self.wait_for_response(seq_st)
        if not st_resp or not st_resp["body"]["stackFrames"]:
            return {"error": "No stack frame. Is it paused?"}
            
        frame_id = st_resp["body"]["stackFrames"][0]["id"]
        
        seq_scopes = self.send("scopes", {"frameId": frame_id})
        loc_ref = self.wait_for_response(seq_scopes)["body"]["scopes"][0]["variablesReference"]
        
        seq_vars = self.send("variables", {"variablesReference": loc_ref})
        variables = self.wait_for_response(seq_vars)["body"]["variables"]
        
        snapshot = {v['name']: v['value'] for v in variables if not v['name'].startswith('__')}
        return snapshot

    def close(self):
        self.running = False
        try:
            self.sock.close()
        except: pass

class DAPGatewayManager:
    def __init__(self):
        self.sessions = {}

    def attach(self, port):
        if port in self.sessions:
            return {"status": "error", "message": f"Port {port} already attached"}
        try:
            client = AsyncDAPClient(port)
            self.sessions[port] = client
            return {"status": "success", "message": f"Attached to {port}"}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    def detach(self, port):
        if port in self.sessions:
            self.sessions[port].close()
            del self.sessions[port]
            return {"status": "success", "message": f"Detached from {port}"}
        return {"status": "error", "message": "Not attached"}

gateway = DAPGatewayManager()

class GatewayAPIHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)
        port = int(qs.get('port', [0])[0])
        
        if parsed.path == '/attach' and port:
            res = gateway.attach(port)
        elif parsed.path == '/detach' and port:
            res = gateway.detach(port)
        elif parsed.path == '/pause' and port:
            if port in gateway.sessions:
                res = {"status": "success"} if gateway.sessions[port].pause() else {"status": "error"}
            else: res = {"status": "error", "message": "Not attached"}
        elif parsed.path == '/resume' and port:
            if port in gateway.sessions:
                res = {"status": "success"} if gateway.sessions[port].resume() else {"status": "error"}
            else: res = {"status": "error", "message": "Not attached"}
        else:
            self.send_response(404)
            self.end_headers()
            return

        self.send_response(200)
        self.send_header('Content-type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps(res).encode('utf-8'))

    def do_GET(self):
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)
        port = int(qs.get('port', [0])[0])
        
        if parsed.path == '/snapshot' and port:
            if port in gateway.sessions:
                client = gateway.sessions[port]
                client.pause()
                data = client.get_snapshot()
                client.resume()
                res = {"status": "success", "snapshot": data}
            else:
                res = {"status": "error", "message": "Not attached"}
        elif parsed.path == '/status':
            res = {"status": "running", "sessions": list(gateway.sessions.keys())}
        else:
            self.send_response(404)
            self.end_headers()
            return
            
        self.send_response(200)
        self.send_header('Content-type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps(res, ensure_ascii=False).encode('utf-8'))

    def log_message(self, format, *args):
        pass

def run():
    print("==================================================")
    print(" [AI Agent DAP Gateway] Started (Port 5680)")
    print("==================================================")
    server = HTTPServer(('0.0.0.0', 5680), GatewayAPIHandler)
    try: server.serve_forever()
    except KeyboardInterrupt: sys.exit(0)

if __name__ == "__main__":
    run()
