# -*- coding: utf-8 -*-
"""
wb_watchdog.py — WorkBuddy 服务看门狗

一个进程管住所有服务：
1. 启动 429 自动重试代理（8788）→ 内部会拉起官方代理（8787）
2. 启动托盘图标（wb_tray.py）
3. 启动更新页面（wb_updater_web.py，8789）
4. 每 20 秒巡检：进程退出 或 端口连续 2 次不通 → 自动重启

日志写入 watchdog.log。
"""

import os
import sys
import time
import socket
import subprocess
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent.resolve()
LOG_FILE = SCRIPT_DIR / "watchdog.log"

# 抑制子进程（git.exe 等）启动失败时弹出的 "应用程序无法正常启动(0xc0000142)" 对话框
if os.name == 'nt':
    try:
        import ctypes
        ctypes.windll.kernel32.SetErrorMode(0x0001 | 0x0002 | 0x8000)
    except Exception:
        pass

# 使用与自身相同的 Python 解释器，保证环境一致（pystray 等依赖都可用）
PYTHON = sys.executable
PYTHONW = str(Path(PYTHON).with_name("pythonw.exe"))
if not Path(PYTHONW).exists():
    PYTHONW = PYTHON

# 服务列表：(名称, 命令参数, 探测端口 or None)
# 代理由 pythonw 启动即可（无窗口后台服务），stdout 已重定向到 proxy-429.log
SERVICES = [
    ("proxy-8788",
     [PYTHONW, str(SCRIPT_DIR / "wb_429_fixer.py"),
      "--port", "8788", "--upstream", "8787", "--max-retries", "15"],
     8788),
    ("tray",
     [PYTHONW, str(SCRIPT_DIR / "wb_tray.py")],
     None),
    ("updater-8789",
     [PYTHONW, str(SCRIPT_DIR / "wb_updater_web.py")],
     8789),
]

CHECK_INTERVAL = 20       # 巡检间隔（秒）
RESTART_DELAY = 5         # 进程挂掉后重启延迟
PORT_FAIL_LIMIT = 2       # 端口连续失败多少次后重启
START_GRACE = 25          # 服务启动后宽限期（秒），期间不做端口判定


def log(msg):
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def port_open(port, timeout=2):
    """检测本地 TCP 端口是否可连接"""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except Exception:
        return False


state = {}  # name -> {"proc", "cmd", "port", "fails", "started_at"}


def start_service(name, cmd, port):
    flags = 0
    if os.name == "nt":
        flags = subprocess.CREATE_NO_WINDOW
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(SCRIPT_DIR),
            creationflags=flags,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        state[name] = {
            "proc": proc, "cmd": cmd, "port": port,
            "fails": 0, "started_at": time.time(),
        }
        log(f"启动 {name} (PID {proc.pid})")
        return True
    except Exception as e:
        log(f"启动 {name} 失败: {e}")
        state[name] = {
            "proc": None, "cmd": cmd, "port": port,
            "fails": PORT_FAIL_LIMIT, "started_at": 0,
        }
        return False


def restart_service(name):
    s = state[name]
    proc = s["proc"]
    if proc is not None and proc.poll() is None:
        try:
            proc.terminate()
            time.sleep(2)
            if proc.poll() is None:
                proc.kill()
        except Exception:
            pass
    start_service(name, s["cmd"], s["port"])


def watchdog_loop():
    log("=" * 40)
    log("看门狗启动")

    # 首次启动：先起代理，给它足够时间拉起上游和绑定端口
    for name, cmd, port in SERVICES:
        start_service(name, cmd, port)
        if name == "proxy-8788":
            time.sleep(START_GRACE)
            if port and not port_open(port):
                log(f"{name} 启动宽限期后端口仍不通，立即重启一次")
                restart_service(name)
                time.sleep(START_GRACE)

    while True:
        time.sleep(CHECK_INTERVAL)
        for name in list(state.keys()):
            s = state[name]
            proc = s["proc"]

            # 1) 进程退出 → 重启
            if proc is None or proc.poll() is not None:
                code = proc.poll() if proc is not None else "None"
                log(f"{name} 进程已退出 (code {code})，{RESTART_DELAY}秒后重启")
                time.sleep(RESTART_DELAY)
                start_service(name, s["cmd"], s["port"])
                continue

            # 2) 端口探测（宽限期内不判定）
            if s["port"] is not None:
                if time.time() - s["started_at"] < START_GRACE:
                    continue
                if port_open(s["port"]):
                    if s["fails"] > 0:
                        log(f"{name} 端口恢复")
                    s["fails"] = 0
                else:
                    s["fails"] += 1
                    log(f"{name} 端口不通 ({s['fails']}/{PORT_FAIL_LIMIT})")
                    if s["fails"] >= PORT_FAIL_LIMIT:
                        log(f"{name} 端口连续 {PORT_FAIL_LIMIT} 次不通，重启")
                        restart_service(name)


if __name__ == "__main__":
    try:
        watchdog_loop()
    except KeyboardInterrupt:
        log("看门狗退出，清理子进程")
        for name, s in state.items():
            proc = s.get("proc")
            if proc is not None:
                try:
                    proc.terminate()
                except Exception:
                    pass
