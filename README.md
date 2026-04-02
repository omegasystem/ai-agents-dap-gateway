# AI Agent DAP Gateway 🚀

> "Why debug in the mud when you can pause the universe?"  
> A zero-intrusion, REST API-driven Debug Adapter Protocol (DAP) gateway designed for AI Agents to dynamically observe, pause, step through, and extract runtime variables from Python applications.

## 🌟 The Problem
AI coding agents (like Claude-Code, OpenClaw, or AutoGPT) cannot natively interact with traditional IDE debuggers (like VS Code). When a script fails or hangs due to a logical error or a `NaN` tensor, AI agents are forced to blindly read `print()` logs or guess the issue. 

## 💡 The Solution: Debugger as a Service (DaaS)
**AI Agent DAP Gateway** turns debugging into a REST API. 
1. **Zero-Intrusion**: Wrap any pure Python script using the Launcher. No `import debugpy` needed in your business logic.
2. **Distributed Mesh**: Support for cross-machine debugging. Gateway and targets can run on different hosts (Windows/Linux/WSL) via auto-IP registration.
3. **SSE Real-Time Push**: Active event streaming for breakpoint hits. No polling required.
4. **Multi-Threaded Concurrency**: Handle multiple SSE listeners and API calls simultaneously.
5. **Time-Stop Snapshotting**: Freeze the execution thread, extract all local variables as a clean JSON payload, and resume execution—all in milliseconds.
6. **Step Execution**: Step over, into, or out of functions with full DAP precision.
7. **Selective Variable Expansion**: Snapshot lists expandable objects with `__ref__` IDs; drill in on demand via `/expand`.
8. **Auth Protection**: Token-based access control via `X-DAP-Token` header and `DAP_AUTH_TOKEN` env var.
9. **Session Lifecycle Management**: Zombie session auto-cleanup and `terminated` event broadcast on target disconnect.

---

## 🛠️ Quick Start

### 1. Install Dependencies
```bash
pip install -r requirements.txt
```

### 2. Start the Gateway (The "Control Plane")
Run this in a background terminal. It listens on Port `5680` for REST API commands.
```bash
python3 dap_gateway.py
```

Optionally enable auth:
```bash
DAP_AUTH_TOKEN=mysecret python3 dap_gateway.py
```

### 3. Launch a Target Script (The "Probe")
The Launcher will automatically find a free port, listen on `0.0.0.0`, detect its own IP, and register itself to the Gateway.
```bash
python3 dap_launcher.py example_target.py
```
*(Optionally use `DAP_GATEWAY_HOST` to specify the Gateway address)*

### 4. Global Event Stream — Single Connection, All Sessions (The "God's Eye")

Monitor every session from one SSE connection. No need to open one stream per process.

```bash
curl -N "http://127.0.0.1:5680/events/global"
```

Example output:
```
data: {"type": "event", "event": "session_attached", "session": "192.168.1.5:45123", "target": "train.py", "body": {"attached_at": "2026-04-02T15:00:00"}}
data: {"type": "event", "event": "stopped", "session": "192.168.1.5:45123", "target": "train.py", "body": {...}}
data: {"type": "event", "event": "terminated", "session": "192.168.1.5:45123", "target": "train.py", "body": {}}
```

### 5. Real-Time Event Listening — Per Session (The "Nervous System")
Subscribe to real-time events (SSE). When a breakpoint is hit, the Gateway pushes the notification instantly. Use `snapshot=true` for automatic variable capture.

```bash
curl -N -H "Accept: text/event-stream" "http://127.0.0.1:5680/events?host=127.0.0.1&port=45123&snapshot=true"
```

### 6. Extract Runtime Variables (The "God Mode")
Manually extract the local variables of the running script via a simple `curl` command.

```bash
curl http://127.0.0.1:5680/snapshot?host=127.0.0.1&port=45123
```

Variables containing objects/dicts/lists are marked with `__expandable__: true` and a `__ref__` ID. Drill in with `/expand`:

```bash
curl "http://127.0.0.1:5680/expand?host=127.0.0.1&port=45123&ref=1234"
```

### 7. Step Execution (The "Single Frame Advance")

```bash
# Step over (next line)
curl -X POST "http://127.0.0.1:5680/step/over?host=127.0.0.1&port=45123&threadId=1"

# Step into function
curl -X POST "http://127.0.0.1:5680/step/in?host=127.0.0.1&port=45123&threadId=1"

# Step out of function
curl -X POST "http://127.0.0.1:5680/step/out?host=127.0.0.1&port=45123&threadId=1"
```

---

## 📡 API Endpoints (Port 5680)

All endpoints (except `/status`) support `?host=<TARGET_IP>` and `?port=<DAP_PORT>`.

| Method | Endpoint | Description |
| :--- | :--- | :--- |
| `GET` | `/status` | List all active attached sessions. |
| `GET` | `/events` | **SSE Stream** (per-session). Pushes real-time events: `stopped`, `terminated`, `gateway_error`. Supports `?snapshot=true`. |
| `GET` | `/events/global` | **Global SSE Stream**. Single connection observes ALL sessions. Every event includes `session` (host:port) and `target` (filename). Lifecycle events: `session_attached`, `session_detached`, `terminated`. No `?host`/`?port` needed. |
| `POST` | `/attach` | Manually attach the Gateway to a remote listening debugpy port. |
| `POST` | `/detach` | Disconnect the Gateway from the target. |
| `POST` | `/pause` | Freeze the target execution thread (Time Stop). |
| `POST` | `/resume` | Resume the target execution thread. |
| `GET` | `/snapshot` | **Auto-Sequence**: Pauses, extracts local variables (with expandable markers), and resumes immediately. |
| `GET` | `/expand` | Expand one level of an expandable variable by `?ref=<id>`. Requires target to be paused. |
| `POST` | `/step/over` | Step over — execute next line. Requires `?threadId=<id>`. |
| `POST` | `/step/in` | Step into — enter the next function call. Requires `?threadId=<id>`. |
| `POST` | `/step/out` | Step out — exit the current function. Requires `?threadId=<id>`. |

---

## 🔐 Authentication

Set the `DAP_AUTH_TOKEN` environment variable to enable token-based auth. All `POST` requests must include the `X-DAP-Token` header.

```bash
# Start gateway with auth
DAP_AUTH_TOKEN=mysecret python3 dap_gateway.py

# Make authenticated requests
curl -X POST -H "X-DAP-Token: mysecret" "http://127.0.0.1:5680/pause?port=45123"
```

If `DAP_AUTH_TOKEN` is not set, all requests are allowed (development mode).

---

## ⚡ REST Overhead Assessment

For AI agent use cases (low-frequency, event-driven), REST + SSE is the right architecture:
- HTTP header cost (~200–400 bytes/req) is negligible at normal agent call rates
- SSE eliminates per-event overhead once the connection is established
- `ThreadingHTTPServer` handles up to ~100 concurrent agents comfortably

**When to upgrade:** If step-debug loops exceed ~50 req/s, migrate the control plane to WebSocket or Unix domain socket to eliminate TCP handshake and header overhead.

---

## 🏗️ Distributed Architecture
The Gateway is designed for distributed environments:
- **`dap_launcher.py`**: Listens on `0.0.0.0` to allow remote connections. Automatically detects the host IP for registration.
- **`dap_gateway.py`**: Uses `ThreadingHTTPServer` for non-blocking concurrent request handling (perfect for Cloudflare Zero Trust tunnels).

---

## 👨‍💻 Developed By
Co-created by Carbon-based S-tier Architect **Leon** & Silicon-based Digital Familiars **Kuro (小黑)** and **Shiro (小白)**.
