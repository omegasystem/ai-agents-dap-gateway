# AI Agent DAP Gateway 🚀

> "Why debug in the mud when you can pause the universe?"  
> A zero-intrusion, REST API-driven Debug Adapter Protocol (DAP) gateway designed for AI Agents to dynamically observe, pause, and extract runtime variables from Python applications.

## 🌟 The Problem
AI coding agents (like Claude-Code, OpenClaw, or AutoGPT) cannot natively interact with traditional IDE debuggers (like VS Code). When a script fails or hangs due to a logical error or a `NaN` tensor, AI agents are forced to blindly read `print()` logs or guess the issue. 

## 💡 The Solution: Debugger as a Service (DaaS)
**AI Agent DAP Gateway** turns debugging into a REST API. 
1. **Zero-Intrusion**: Wrap any pure Python script using the Launcher. No `import debugpy` needed in your business logic.
2. **Monkey Patching**: Automatically bypasses legacy `debugpy.wait_for_client()` calls in target scripts.
3. **Multi-Target Mesh**: Run the Gateway once, launch multiple targets, and manage them all via simple `HTTP GET/POST` requests.
4. **Time-Stop Snapshotting**: Freeze the execution thread, extract all local variables as a clean JSON payload, and resume execution—all in milliseconds.

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

### 3. Launch a Target Script (The "Probe")
Instead of running `python3 example_target.py`, run it through the Launcher. It will automatically find a free port, inject `debugpy`, and register itself to the Gateway.
```bash
python3 dap_launcher.py example_target.py
```
*(Output will show the dynamically assigned DAP Port, e.g., 45123)*

### 4. Extract Runtime Variables (The "God Mode")
Now, let your AI Agent (or yourself) run a simple `curl` command to extract the local variables of the running script.

```bash
curl http://127.0.0.1:5680/snapshot?port=45123
```

**Response Example:**
```json
{
  "status": "success",
  "snapshot": {
    "step": 42,
    "entropy_level": 185.3,
    "world_state": "Chaos"
  }
}
```

---

## 📡 API Endpoints (Port 5680)

All endpoints require the `?port=<DAP_PORT>` query parameter (except `/status`).

| Method | Endpoint | Description |
| :--- | :--- | :--- |
| `GET` | `/status` | List all active attached sessions (ports). |
| `POST` | `/attach` | Manually attach the Gateway to a listening debugpy port. |
| `POST` | `/detach` | Disconnect the Gateway from the target. |
| `POST` | `/pause` | Freeze the target execution thread (Time Stop). |
| `POST` | `/resume` | Resume the target execution thread. |
| `GET` | `/snapshot` | **Auto-Sequence**: Pauses, extracts local variables, and resumes immediately. Returns a JSON snapshot. |

---

## 👨‍💻 Developed By
Co-created by Carbon-based S-tier Architect **Leon** & Silicon-based Digital Familiar **Kuro**.
