# -*- coding: utf-8 -*-
"""
WorkBuddy Proxy System Tray Manager
Features: check status, open dashboard, start/stop service
"""
import os
import sys
import webbrowser
import urllib.request
import subprocess
from pathlib import Path

import pystray
from pystray import MenuItem as item
from PIL import Image

PORT = 8788
UPDATE_PORT = 8789
SCRIPT_DIR = Path(__file__).parent.resolve()
ICON_PATH = SCRIPT_DIR / 'wb-icon.ico'


def load_icon():
    """Load icon from ICO file"""
    try:
        return Image.open(str(ICON_PATH))
    except Exception as e:
        print(f'Failed to load icon: {e}')
        # Fallback: simple generated icon
        from PIL import ImageDraw
        size = 64
        img = Image.new('RGBA', (size, size), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        draw.ellipse([4, 4, size-4, size-4], fill=(0, 120, 212, 255))
        draw.rectangle([20, 20, 28, 44], fill=(255,255,255,255))
        draw.rectangle([36, 20, 44, 44], fill=(255,255,255,255))
        return img


def check_status():
    """Check if service is running"""
    try:
        req = urllib.request.Request(f'http://127.0.0.1:{PORT}/', method='GET')
        with urllib.request.urlopen(req, timeout=3) as resp:
            return resp.status == 200
    except Exception:
        return False


def start_service():
    """Start proxy service in background"""
    try:
        flags = 0
        if os.name == 'nt':
            flags = subprocess.CREATE_NO_WINDOW
        subprocess.Popen(
            [sys.executable, str(SCRIPT_DIR / 'wb_proxy.py'), '--port', str(PORT)],
            cwd=str(SCRIPT_DIR),
            creationflags=flags
        )
        return True
    except Exception as e:
        print(f'Start failed: {e}')
        return False


def stop_service():
    """Stop proxy service"""
    try:
        if os.name == 'nt':
            # Only kill wb_proxy, not the tray itself
            for proc in get_proxy_processes():
                subprocess.run(['taskkill', '/f', '/pid', str(proc['pid'])],
                             capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
        return True
    except Exception as e:
        print(f'Stop failed: {e}')
        return False


def get_proxy_processes():
    """Find all wb_proxy processes"""
    procs = []
    try:
        if os.name == 'nt':
            result = subprocess.run(
                ['wmic', 'process', 'where', "name like '%python%'", 'get', 'processid,commandline', '/format:csv'],
                capture_output=True, text=True, creationflags=subprocess.CREATE_NO_WINDOW
            )
            for line in result.stdout.strip().split('\n'):
                if 'wb_proxy' in line.lower():
                    parts = line.strip().split(',')
                    if len(parts) >= 3:
                        try:
                            pid = int(parts[-1])
                            procs.append({'pid': pid, 'cmd': line})
                        except (ValueError, IndexError):
                            pass
    except Exception:
        pass
    return procs


def on_open_dashboard(icon, item):
    """Open dashboard in browser"""
    webbrowser.open(f'http://127.0.0.1:{PORT}/')


def on_open_updater(icon, item):
    """Open update page in browser"""
    webbrowser.open(f'http://127.0.0.1:{UPDATE_PORT}/')


def on_check_status(icon, item):
    """Show status notification"""
    running = check_status()
    if running:
        icon.notify('Service is running', 'WorkBuddy Proxy')
    else:
        icon.notify('Service is NOT running', 'WorkBuddy Proxy')


def on_start(icon, item):
    """Start service"""
    if check_status():
        icon.notify('Already running', 'WorkBuddy Proxy')
        return
    start_service()
    import time
    time.sleep(4)
    if check_status():
        icon.notify('Service started', 'WorkBuddy Proxy')
    else:
        icon.notify('Start failed, please check', 'WorkBuddy Proxy')


def on_stop(icon, item):
    """Stop service"""
    stop_service()
    import time
    time.sleep(2)
    icon.notify('Service stopped', 'WorkBuddy Proxy')


def on_restart(icon, item):
    """Restart service"""
    stop_service()
    import time
    time.sleep(2)
    start_service()
    time.sleep(4)
    if check_status():
        icon.notify('Service restarted', 'WorkBuddy Proxy')
    else:
        icon.notify('Restart failed, please check', 'WorkBuddy Proxy')


def on_quit(icon, item):
    """Quit tray (keep service running)"""
    icon.stop()


def on_double_click(icon, button):
    """Double-click opens dashboard"""
    if button == pystray.MouseButton.LEFT:
        webbrowser.open(f'http://127.0.0.1:{PORT}/')


def main():
    # Check initial status
    running = check_status()
    status_text = 'Status: Running' if running else 'Status: Stopped'

    menu = pystray.Menu(
        item(status_text, lambda: None, enabled=False),
        pystray.Menu.SEPARATOR,
        item('Open Dashboard', on_open_dashboard, default=True),
        item('Check for Updates...', on_open_updater),
        item(f'API: http://127.0.0.1:{PORT}/v1', lambda: None, enabled=False),
        pystray.Menu.SEPARATOR,
        item('Start Service', on_start),
        item('Stop Service', on_stop),
        item('Restart Service', on_restart),
        pystray.Menu.SEPARATOR,
        item('Quit Tray (keep service)', on_quit),
    )

    icon = pystray.Icon(
        'WorkBuddyProxy',
        load_icon(),
        'WorkBuddy Proxy',
        menu
    )

    icon.on_double_click = on_double_click

    # Show a welcome notification
    try:
        icon.notify('WorkBuddy Proxy is ready', 'WorkBuddy Proxy')
    except Exception:
        pass

    icon.run()


if __name__ == '__main__':
    main()
