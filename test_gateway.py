#!/usr/bin/env python3
"""
DAP Gateway Integration Test Suite  —  FSM Edition
=====================================================
用有限狀態機（FSM）驅動的自動化整合測試。
每個 State 封裝自己的 enter() 動作與 next() 轉移邏輯，
測試流程一目瞭然、易於擴充、失敗可精確定位。

用法：
    python3 test_gateway.py [--gateway 127.0.0.1:5680] [--wait 30]

前提：
    - dap_gateway.py 已在背景執行（port 5680）
    - dap_launcher.py example_target.py 已執行
"""

import sys
import os
import time
import json
import threading
import argparse
import urllib.request
import urllib.error
from enum import Enum, auto
from queue import Queue, Empty

# ──────────────────────────────────────────
# 組態
# ──────────────────────────────────────────
GATEWAY_BASE   = "http://127.0.0.1:5680"
TARGET_SCRIPT  = os.path.join(os.path.dirname(__file__), "example_target.py")
SSE_TIMEOUT    = 15   # 等待 SSE 事件的秒數
BYPASS_WAIT    = 8    # 確認 bypass 的觀察秒數

# ──────────────────────────────────────────
# 顏色輸出
# ──────────────────────────────────────────
GREEN  = "\033[92m"
RED    = "\033[91m"
YELLOW = "\033[93m"
CYAN   = "\033[96m"
RESET  = "\033[0m"
BOLD   = "\033[1m"
DIM    = "\033[2m"

def ok(msg):    print(f"    {GREEN}✓{RESET} {msg}")
def fail(msg):  print(f"    {RED}✗{RESET} {msg}")
def info(msg):  print(f"    {YELLOW}→{RESET} {msg}")
def state_hdr(name): print(f"\n{BOLD}{CYAN}┌─ {name} {RESET}{DIM}{'─'*(46-len(name))}{RESET}")


# ──────────────────────────────────────────
# HTTP 工具
# ──────────────────────────────────────────
def api_get(path, params=None):
    url = f"{GATEWAY_BASE}{path}"
    if params:
        url += "?" + "&".join(f"{k}={v}" for k, v in params.items())
    try:
        with urllib.request.urlopen(url, timeout=8) as r:
            return json.loads(r.read())
    except Exception as e:
        return {"error": str(e)}

def api_post(path, params=None):
    url = f"{GATEWAY_BASE}{path}"
    if params:
        url += "?" + "&".join(f"{k}={v}" for k, v in params.items())
    try:
        req = urllib.request.Request(url, method="POST")
        with urllib.request.urlopen(req, timeout=8) as r:
            return json.loads(r.read())
    except Exception as e:
        return {"error": str(e)}


# ──────────────────────────────────────────
# SSE 監聽器（背景 thread）
# ──────────────────────────────────────────
class SSEListener:
    def __init__(self):
        self.events  = Queue()
        self._stop   = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self):
        url = f"{GATEWAY_BASE}/events/global"
        try:
            req = urllib.request.Request(url, headers={"Accept": "text/event-stream"})
            with urllib.request.urlopen(req, timeout=None) as r:
                while not self._stop.is_set():
                    line = r.readline().decode("utf-8", errors="replace")
                    if line.startswith("data:"):
                        try:
                            self.events.put(json.loads(line[5:].strip()))
                        except Exception:
                            pass
        except Exception:
            pass

    def wait_for(self, event_name, timeout=SSE_TIMEOUT, extra_check=None):
        """等待指定 event，可附加額外條件。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                ev = self.events.get(timeout=1)
                if ev.get("event") == event_name:
                    if extra_check is None or extra_check(ev):
                        return ev
            except Empty:
                pass
        return None

    def drain(self):
        while not self.events.empty():
            try:
                self.events.get_nowait()
            except Empty:
                break

    def stop(self):
        self._stop.set()


# ──────────────────────────────────────────
# FSM 狀態定義
# ──────────────────────────────────────────
class S(Enum):
    INIT             = auto()  # 確認 Gateway 在線
    WAIT_SESSION     = auto()  # 等待 debug session 上線
    SET_BREAKPOINT   = auto()  # 設置斷點 lines 9,11
    WAIT_HIT         = auto()  # 等待 SSE stopped reason=breakpoint
    QUERY_CACHE      = auto()  # 驗證 /breakpoints 快取
    DISABLE          = auto()  # disable + resume
    VERIFY_BYPASS    = auto()  # 觀察 8s 確認 bypass 有效
    ENABLE           = auto()  # enable + 等待斷點重觸發
    WAIT_RESTORE_HIT = auto()  # 等待 enable 後的 stopped 事件
    IDEMPOTENT       = auto()  # 重複 disable 冪等性
    CLEAR            = auto()  # 清除斷點並驗證
    DONE             = auto()  # 終止（成功）
    ABORT            = auto()  # 終止（失敗）


# ──────────────────────────────────────────
# FSM 上下文（共享資料）
# ──────────────────────────────────────────
class Context:
    def __init__(self, wait_timeout):
        self.host        = None
        self.port        = None
        self.target_file = os.path.abspath(TARGET_SCRIPT)
        self.wait_timeout = wait_timeout
        self.sse         = SSEListener()
        self.passed      = 0
        self.failed      = 0
        # 參數快捷鍵
        self.p   = {}    # host + port
        self.pf  = {}    # host + port + file

    def set_session(self, host, port):
        self.host = host
        self.port = port
        self.p    = {"host": host, "port": str(port)}
        self.pf   = {"host": host, "port": str(port), "file": self.target_file}

    def ok(self, label):
        ok(label)
        self.passed += 1

    def fail(self, label, detail=""):
        fail(f"{label}  {detail}".strip())
        self.failed += 1

    def check(self, label, cond, detail=""):
        if cond:
            self.ok(label)
        else:
            self.fail(label, detail)
        return cond

    def check_res(self, label, res, key="status", expected="success"):
        return self.check(label, res.get(key) == expected, f"→ {res}")

    def summary(self):
        total = self.passed + self.failed
        print()
        print(f"{BOLD}{'═'*50}{RESET}")
        color = GREEN if self.failed == 0 else RED
        print(f"{BOLD}{color}結果：{self.passed}/{total} 通過{RESET}", end="")
        if self.failed:
            print(f"  {RED}（{self.failed} 失敗）{RESET}")
        else:
            print(f"  🎉")
        print(f"{BOLD}{'═'*50}{RESET}")
        return self.failed == 0


# ──────────────────────────────────────────
# 各狀態實作
# ──────────────────────────────────────────

def state_init(ctx: Context) -> S:
    state_hdr("1. INIT  基本連線")
    res = api_get("/status")
    if not ctx.check("Gateway 在線", res.get("status") == "running", f"→ {res}"):
        return S.ABORT
    return S.WAIT_SESSION


def state_wait_session(ctx: Context) -> S:
    state_hdr("2. WAIT_SESSION  等待 session")
    info(f"最多等 {ctx.wait_timeout}s...")
    deadline = time.time() + ctx.wait_timeout
    while time.time() < deadline:
        res = api_get("/status")
        sessions = res.get("sessions", {})
        if sessions:
            key = next(iter(sessions))
            host, port = key.rsplit(":", 1)
            ctx.set_session(host, int(port))
            ctx.ok(f"Session 已偵測：{host}:{port}")
            return S.SET_BREAKPOINT
        time.sleep(1)
    ctx.fail("Session 上線逾時")
    return S.ABORT


def state_set_breakpoint(ctx: Context) -> S:
    state_hdr("3. SET_BREAKPOINT  設置斷點")
    ctx.sse.drain()
    res = api_post("/breakpoint", {**ctx.pf, "lines": "9,11"})
    if not ctx.check_res("POST /breakpoint", res):
        return S.ABORT

    bps = res.get("breakpoints", [])
    ctx.check("回傳 2 個斷點", len(bps) == 2, f"got {len(bps)}")
    ctx.check("斷點均 verified",
              all(bp.get("verified") for bp in bps),
              str([bp.get("verified") for bp in bps]))
    return S.WAIT_HIT


def state_wait_hit(ctx: Context) -> S:
    state_hdr("4. WAIT_HIT  等待斷點觸發")
    info(f"等待 SSE stopped(reason=breakpoint)，最多 {SSE_TIMEOUT}s...")
    ev = ctx.sse.wait_for(
        "stopped",
        extra_check=lambda e: e.get("body", {}).get("reason") == "breakpoint"
    )
    ctx.check("SSE 收到 breakpoint stopped", ev is not None,
              "(timeout)" if ev is None else "")
    return S.QUERY_CACHE if ev else S.ABORT


def state_query_cache(ctx: Context) -> S:
    state_hdr("5. QUERY_CACHE  查詢斷點快取")
    res = api_get("/breakpoints", ctx.pf)
    ctx.check_res("GET /breakpoints", res)
    ctx.check("快取 lines = [9, 11]",
              sorted(res.get("lines", [])) == [9, 11],
              f"got {res.get('lines')}")
    ctx.check("disabled = False", res.get("disabled") == False,
              f"got {res.get('disabled')}")
    return S.DISABLE


def state_disable(ctx: Context) -> S:
    state_hdr("6. DISABLE  bypass 斷點")
    res = api_post("/breakpoints/disable", ctx.p)
    ctx.check_res("POST /breakpoints/disable", res)
    ctx.check("disabled_files 包含目標檔",
              ctx.target_file in res.get("disabled_files", []),
              str(res.get("disabled_files")))
    ctx.check("failed 為空", res.get("failed") == [], str(res.get("failed")))

    # resume 讓程式繼續跑穿 bypass 視窗
    r2 = api_post("/resume", ctx.p)
    ctx.check_res("Resume 成功", r2)

    ctx.sse.drain()
    return S.VERIFY_BYPASS


def state_verify_bypass(ctx: Context) -> S:
    state_hdr("7. VERIFY_BYPASS  確認無觸發")
    info(f"靜觀 {BYPASS_WAIT}s，確認斷點已 bypass...")
    time.sleep(BYPASS_WAIT)
    ctx.check("Bypass 期間無新 stopped 事件", ctx.sse.events.empty(),
              "（SSE 有收到新事件）")
    return S.ENABLE


def state_enable(ctx: Context) -> S:
    state_hdr("8. ENABLE  恢復斷點")
    ctx.sse.drain()
    res = api_post("/breakpoints/enable", ctx.p)
    ctx.check_res("POST /breakpoints/enable", res)
    ctx.check("restored_files 包含目標檔",
              ctx.target_file in res.get("restored_files", []),
              str(res.get("restored_files")))
    ctx.check("failed 為空", res.get("failed") == [], str(res.get("failed")))
    return S.WAIT_RESTORE_HIT


def state_wait_restore_hit(ctx: Context) -> S:
    state_hdr("9. WAIT_RESTORE_HIT  等待恢復後觸發")
    info(f"等待斷點再次觸發，最多 {SSE_TIMEOUT}s...")
    ev = ctx.sse.wait_for(
        "stopped",
        extra_check=lambda e: e.get("body", {}).get("reason") == "breakpoint"
    )
    ctx.check("Enable 後斷點重新觸發", ev is not None,
              "(timeout)" if ev is None else "")
    return S.IDEMPOTENT


def state_idempotent(ctx: Context) -> S:
    state_hdr("10. IDEMPOTENT  冪等性")
    api_post("/breakpoints/disable", ctx.p)          # 第一次
    res = api_post("/breakpoints/disable", ctx.p)    # 第二次
    ctx.check("重複 disable → Already disabled",
              res.get("message") == "Already disabled", str(res))
    return S.CLEAR


def state_clear(ctx: Context) -> S:
    state_hdr("11. CLEAR  清除斷點")
    api_post("/breakpoints/enable", ctx.p)           # 先恢復，再清除
    res = api_post("/breakpoint/clear", ctx.pf)
    ctx.check_res("POST /breakpoint/clear", res)

    res2 = api_get("/breakpoints", ctx.pf)
    ctx.check("Clear 後 lines 為空", res2.get("lines") == [], str(res2.get("lines")))

    api_post("/resume", ctx.p)
    return S.DONE


# ──────────────────────────────────────────
# FSM 轉移表
# ──────────────────────────────────────────
TRANSITIONS = {
    S.INIT:             state_init,
    S.WAIT_SESSION:     state_wait_session,
    S.SET_BREAKPOINT:   state_set_breakpoint,
    S.WAIT_HIT:         state_wait_hit,
    S.QUERY_CACHE:      state_query_cache,
    S.DISABLE:          state_disable,
    S.VERIFY_BYPASS:    state_verify_bypass,
    S.ENABLE:           state_enable,
    S.WAIT_RESTORE_HIT: state_wait_restore_hit,
    S.IDEMPOTENT:       state_idempotent,
    S.CLEAR:            state_clear,
}


# ──────────────────────────────────────────
# FSM 執行器
# ──────────────────────────────────────────
def run_fsm(ctx: Context) -> bool:
    current = S.INIT
    while current not in (S.DONE, S.ABORT):
        handler = TRANSITIONS.get(current)
        if handler is None:
            print(f"{RED}✗ 未知狀態：{current}{RESET}")
            break
        try:
            current = handler(ctx)
        except Exception as exc:
            print(f"{RED}✗ 狀態 {current.name} 拋出例外：{exc}{RESET}")
            current = S.ABORT

    if current == S.ABORT:
        print(f"\n{RED}{BOLD}FSM 因失敗提前終止。{RESET}")

    return ctx.summary()


# ──────────────────────────────────────────
# 主程式
# ──────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="DAP Gateway FSM Integration Tests")
    parser.add_argument("--gateway", default="127.0.0.1:5680")
    parser.add_argument("--wait",    type=int, default=30, help="等待 session 上線的秒數")
    args = parser.parse_args()

    gw_host, gw_port = args.gateway.split(":")
    global GATEWAY_BASE
    GATEWAY_BASE = f"http://{gw_host}:{gw_port}"

    print(f"\n{BOLD}DAP Gateway Integration Tests  —  FSM Edition{RESET}")
    print(f"{DIM}Gateway : {GATEWAY_BASE}{RESET}")
    print(f"{DIM}Target  : {TARGET_SCRIPT}{RESET}")

    ctx = Context(wait_timeout=args.wait)
    time.sleep(0.3)   # 讓 SSE 連線建立

    success = run_fsm(ctx)
    ctx.sse.stop()
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
