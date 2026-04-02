"""
AI Agent DAP Gateway — REST overhead evaluation (v2.0)

REST overhead assessment for high-frequency debug scenarios:
- HTTP header cost: ~200-400 bytes per request. For snapshot/step ops (low freq),
  this is negligible. For high-freq polling (>100 req/s), it becomes measurable.
- ThreadingHTTPServer: spawns one thread per connection. Adequate for moderate
  concurrency (<50 clients). Under heavy load, thread creation overhead dominates.
- SSE (current) avoids per-event HTTP overhead once connection is established —
  this is already the right call for event streaming.

Recommendation:
- Keep REST + SSE for current use case (AI agent driven, not high-freq polling).
- If step-debugging loops exceed ~50 req/s, migrate control plane to WebSocket
  or Unix domain socket to eliminate header overhead and TCP handshake cost.
- ThreadingHTTPServer is sufficient unless gateway serves >100 concurrent agents;
  at that scale, switch to asyncio-based server (e.g. aiohttp).
"""

import socket
import json
import time
import sys
import os
import threading
from queue import Queue, Empty
from http.server import HTTPServer, BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs


class AsyncDAPClient:
    """Async DAP Client (one instance per target)"""
    def __init__(self, host, port, event_callback=None, on_disconnect=None):
        self.host = host
        self.port = port
        self.event_callback = event_callback
        self.on_disconnect = on_disconnect
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        print(f"[*] Connecting to DAP Target at {host}:{port}...")
        self.sock.connect((host, port))
        self.seq = 1
        self.responses = {}
        self.events = Queue()
        self.running = True
        self.op_lock = threading.Lock()
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
                    if not data:
                        if self.on_disconnect:
                            self.on_disconnect(self.host, self.port)
                        return
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
                    if self.event_callback and msg.get("event") == "stopped":
                        self.event_callback(self.host, self.port, msg)
            except Exception as e:
                if self.event_callback:
                    error_event = {
                        "type": "event",
                        "event": "gateway_error",
                        "body": {"message": str(e)}
                    }
                    self.event_callback(self.host, self.port, error_event)
                if self.on_disconnect:
                    self.on_disconnect(self.host, self.port)
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
        if resp and resp.get("success") and resp["body"]["threads"]:
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

    def step(self, command, thread_id):
        """Execute a DAP step command and wait for stopped event."""
        self.send(command, {"threadId": thread_id})
        return self.wait_for_event("stopped")

    def get_snapshot(self):
        try:
            tid = self.get_thread_id()
            if not tid: return {"error": "No thread found"}
            seq_st = self.send("stackTrace", {"threadId": tid})
            st_resp = self.wait_for_response(seq_st)
            if not st_resp or not st_resp.get("success") or not st_resp.get("body", {}).get("stackFrames"):
                return {"error": "No stack frame. Is it paused?"}
            frame_id = st_resp["body"]["stackFrames"][0]["id"]
            seq_scopes = self.send("scopes", {"frameId": frame_id})
            scopes_resp = self.wait_for_response(seq_scopes)
            if not scopes_resp or not scopes_resp.get("success"):
                return {"error": "Failed to get scopes"}
            loc_ref = scopes_resp["body"]["scopes"][0]["variablesReference"]
            seq_vars = self.send("variables", {"variablesReference": loc_ref})
            vars_resp = self.wait_for_response(seq_vars)
            if not vars_resp or not vars_resp.get("success"):
                return {"error": "Failed to get variables"}
            variables = vars_resp["body"]["variables"]
            snapshot = {}
            for v in variables:
                if v['name'].startswith('__'):
                    continue
                entry = {"value": v['value']}
                if v.get('variablesReference', 0) > 0:
                    entry['__expandable__'] = True
                    entry['__ref__'] = v['variablesReference']
                snapshot[v['name']] = entry
            return snapshot
        except Exception as e:
            return {"error": f"Snapshot internal error: {str(e)}"}

    def expand_ref(self, variables_reference):
        """Expand one level of a variablesReference."""
        seq_vars = self.send("variables", {"variablesReference": variables_reference})
        vars_resp = self.wait_for_response(seq_vars)
        if not vars_resp or not vars_resp.get("success"):
            return {"error": "Failed to expand variables"}
        variables = vars_resp["body"]["variables"]
        result = {}
        for v in variables:
            entry = {"value": v['value']}
            if v.get('variablesReference', 0) > 0:
                entry['__expandable__'] = True
                entry['__ref__'] = v['variablesReference']
            result[v['name']] = entry
        return result

    def close(self):
        self.running = False
        try:
            self.sock.close()
        except: pass


class DAPGatewayManager:
    def __init__(self):
        self.sessions = {}
        self.session_meta = {}   # key -> {"target": filename, "attached_at": timestamp}
        self.event_queues = {}
        self.lock = threading.Lock()

    def _broadcast_event(self, host, port, event_msg):
        key = f"{host}:{port}"
        with self.lock:
            if key in self.event_queues:
                for q in self.event_queues[key]:
                    q.put(event_msg)

    def _on_disconnect(self, host, port):
        key = f"{host}:{port}"
        meta = self.session_meta.get(key, {})
        terminated = {"type": "event", "event": "terminated", "body": {"target": meta.get("target", "unknown")}}
        with self.lock:
            self.sessions.pop(key, None)
            self.session_meta.pop(key, None)
            if key in self.event_queues:
                for q in self.event_queues[key]:
                    q.put(terminated)

    def attach(self, host, port, target=None):
        key = f"{host}:{port}"
        with self.lock:
            if key in self.sessions:
                return {"status": "error", "message": f"Target {key} already attached"}
        try:
            client = AsyncDAPClient(
                host, port,
                event_callback=self._broadcast_event,
                on_disconnect=self._on_disconnect
            )
            with self.lock:
                self.sessions[key] = client
                self.session_meta[key] = {
                    "target": target or "unknown",
                    "attached_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                }
            return {"status": "success", "message": f"Attached to {key}", "target": target}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    def detach(self, host, port):
        key = f"{host}:{port}"
        with self.lock:
            if key in self.sessions:
                self.sessions[key].close()
                del self.sessions[key]
                return {"status": "success", "message": f"Detached from {key}"}
        return {"status": "error", "message": "Not attached"}


gateway = DAPGatewayManager()


class GatewayAPIHandler(BaseHTTPRequestHandler):

    def _check_auth(self):
        expected = os.environ.get('DAP_AUTH_TOKEN')
        if not expected:
            return True
        provided = self.headers.get('X-DAP-Token', '')
        if provided == expected:
            return True
        self.send_response(401)
        self.send_header('Content-type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps({"status": "unauthorized"}).encode('utf-8'))
        return False

    def do_POST(self):
        if not self._check_auth():
            return
        try:
            parsed = urlparse(self.path)
            qs = parse_qs(parsed.query)
            port = int(qs.get('port', [0])[0])
            host = qs.get('host', ['127.0.0.1'])[0]
            key = f"{host}:{port}"

            if parsed.path == '/attach' and port:
                target = qs.get('target', [None])[0]
                res = gateway.attach(host, port, target=target)
            elif parsed.path == '/detach' and port:
                res = gateway.detach(host, port)
            elif parsed.path == '/pause' and port:
                with gateway.lock:
                    client = gateway.sessions.get(key)
                res = {"status": "success"} if client and client.pause() else {"status": "error"}
            elif parsed.path == '/resume' and port:
                with gateway.lock:
                    client = gateway.sessions.get(key)
                res = {"status": "success"} if client and client.resume() else {"status": "error"}
            elif parsed.path in ('/step/over', '/step/in', '/step/out') and port:
                step_map = {
                    '/step/over': 'next',
                    '/step/in': 'stepIn',
                    '/step/out': 'stepOut',
                }
                dap_cmd = step_map[parsed.path]
                thread_id = qs.get('threadId', [None])[0]
                if not thread_id:
                    res = {"status": "error", "message": "threadId required"}
                else:
                    with gateway.lock:
                        client = gateway.sessions.get(key)
                    if not client:
                        res = {"status": "error", "message": "Not attached"}
                    else:
                        with client.op_lock:
                            stopped = client.step(dap_cmd, int(thread_id))
                        if stopped:
                            res = {"status": "success", "stopped": stopped}
                        else:
                            res = {"status": "error", "message": "Timed out waiting for stopped event"}
            else:
                self.send_response(404)
                self.end_headers()
                return

            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps(res).encode('utf-8'))
        except Exception as e:
            self.send_response(500)
            self.end_headers()
            self.wfile.write(json.dumps({"status": "error", "message": str(e)}).encode('utf-8'))

    def do_GET(self):
        try:
            parsed = urlparse(self.path)
            qs = parse_qs(parsed.query)
            port = int(qs.get('port', [0])[0])
            host = qs.get('host', ['127.0.0.1'])[0]
            snapshot_flag = qs.get('snapshot', ['false'])[0].lower() == 'true'
            key = f"{host}:{port}"

            if parsed.path == '/events' and port:
                self.send_response(200)
                self.send_header('Content-type', 'text/event-stream')
                self.send_header('Cache-Control', 'no-cache')
                self.send_header('Connection', 'keep-alive')
                self.end_headers()
                q = Queue()
                with gateway.lock:
                    if key not in gateway.event_queues:
                        gateway.event_queues[key] = []
                    gateway.event_queues[key].append(q)
                try:
                    while True:
                        try:
                            event_msg = q.get(timeout=30)
                            if snapshot_flag and event_msg.get('event') == 'stopped':
                                with gateway.lock:
                                    client = gateway.sessions.get(key)
                                if client:
                                    event_msg['snapshot'] = client.get_snapshot()
                            payload = f"data: {json.dumps(event_msg, ensure_ascii=False)}\n\n"
                            self.wfile.write(payload.encode('utf-8'))
                            self.wfile.flush()
                        except Empty:
                            self.wfile.write(b": keep-alive\n\n")
                            self.wfile.flush()
                except (ConnectionResetError, BrokenPipeError):
                    pass
                finally:
                    with gateway.lock:
                        if key in gateway.event_queues:
                            gateway.event_queues[key].remove(q)
                return

            elif parsed.path == '/snapshot' and port:
                with gateway.lock:
                    client = gateway.sessions.get(key)
                if client:
                    with client.op_lock:
                        if client.pause():
                            data = client.get_snapshot()
                            client.resume()
                            res = {"status": "success", "snapshot": data}
                        else:
                            res = {"status": "error", "message": "Pause failed"}
                else:
                    res = {"status": "error", "message": "Not attached"}

            elif parsed.path == '/expand' and port:
                ref = qs.get('ref', [None])[0]
                if not ref:
                    res = {"status": "error", "message": "ref required"}
                else:
                    with gateway.lock:
                        client = gateway.sessions.get(key)
                    if not client:
                        res = {"status": "error", "message": "Not attached"}
                    else:
                        expanded = client.expand_ref(int(ref))
                        res = {"status": "success", "variables": expanded}

            elif parsed.path == '/sessions':
                with gateway.lock:
                    res = {
                        "status": "running",
                        "sessions": {
                            key: gateway.session_meta.get(key, {})
                            for key in gateway.sessions
                        }
                    }
            elif parsed.path == '/status':
                with gateway.lock:
                    res = {
                        "status": "running",
                        "sessions": {
                            key: gateway.session_meta.get(key, {})
                            for key in gateway.sessions
                        }
                    }
            else:
                self.send_response(404)
                self.end_headers()
                return

            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps(res, ensure_ascii=False).encode('utf-8'))
        except Exception as e:
            self.send_response(500)
            self.end_headers()
            self.wfile.write(json.dumps({"status": "error", "message": str(e)}).encode('utf-8'))

    def log_message(self, format, *args):
        pass


def run():
    print("==================================================")
    print(" [AI Agent DAP Gateway] Multi-threaded v2.0")
    print(" SSE supports ?snapshot=true for auto-capture")
    print("==================================================")
    server = ThreadingHTTPServer(('0.0.0.0', 5680), GatewayAPIHandler)
    try: server.serve_forever()
    except KeyboardInterrupt: sys.exit(0)


if __name__ == "__main__":
    run()
