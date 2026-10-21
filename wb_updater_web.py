# -*- coding: utf-8 -*-
"""
WorkBuddy Proxy 一键更新服务
独立的小 Web 页面，提供可视化更新功能
端口: 8789

网络策略（国内 GitHub 直连被重置时自动回退）：
  1. git fetch 直连 origin
  2. git fetch 走镜像前缀（ghproxy.imciel.com 等）
  3. 压缩包兜底（codeload 直连 / gh-proxy.com 镜像）
更新后由看门狗自动拉起 429 重试代理和官方代理。
"""
import os
import sys
import io
import json
import shutil
import tarfile
import tempfile
import subprocess
import threading
import time
import urllib.request
import urllib.error
from pathlib import Path
from http.server import HTTPServer, BaseHTTPRequestHandler

PORT = 8789
SCRIPT_DIR = Path(__file__).parent.resolve()
MAIN_PORT = 8788

# 抑制 git.exe 启动失败时弹出的 "应用程序无法正常启动(0xc0000142)" 对话框
if os.name == 'nt':
    try:
        import ctypes
        ctypes.windll.kernel32.SetErrorMode(0x0001 | 0x0002 | 0x8000)
    except Exception:
        pass

# 已实测可用的 git 镜像前缀（对 https://github.com/... 整体加前缀）
GIT_MIRROR_PREFIXES = [
    'https://ghproxy.imciel.com/',
]
# 压缩包下载前缀（顺序尝试；空串 = codeload 直连）
ARCHIVE_PREFIXES = [
    '',  # https://codeload.github.com 国内当前可直连
    'https://gh-proxy.com/',
]
# 更新时必须保留、不能被覆盖或删除的本地目录/文件
PROTECTED_NAMES = {'accounts', 'usage', '.git'}

# 更新状态
_update_status = {
    'state': 'idle',  # idle, checking, updating, restarting, success, error
    'message': '',
    'current_version': '',
    'latest_version': '',
    'progress': 0,
    'log': []
}
_lock = threading.Lock()


def log(msg):
    with _lock:
        _update_status['log'].append(f'[{time.strftime("%H:%M:%S")}] {msg}')
        if len(_update_status['log']) > 80:
            _update_status['log'] = _update_status['log'][-80:]


def run_git(args, cwd=None, timeout=180):
    """运行 git 命令（字节读取，兼容中文 Windows 的 GBK 输出）"""
    if cwd is None:
        cwd = str(SCRIPT_DIR)
    env = {**os.environ, 'GIT_TERMINAL_PROMPT': '0', 'LC_ALL': 'C', 'LANG': 'C'}
    try:
        result = subprocess.run(
            ['git'] + args,
            cwd=cwd,
            capture_output=True,
            timeout=timeout,
            env=env,
        )
        out = result.stdout.decode('utf-8', 'replace').strip()
        err = result.stderr.decode('utf-8', 'replace').strip()
        return result.returncode, out, err
    except Exception as e:
        return -1, '', str(e)


def get_branch():
    code, out, _ = run_git(['rev-parse', '--abbrev-ref', 'HEAD'])
    if code == 0 and out and out != 'HEAD':
        return out.strip()
    return 'main'


def get_origin_url():
    """获取 origin URL，多命令回退，保证在异常环境下也能拿到"""
    code, out, err = run_git(['remote', 'get-url', 'origin'])
    if code == 0 and out.strip():
        return out.strip()
    code2, out2, _ = run_git(['config', '--get', 'remote.origin.url'])
    if code2 == 0 and out2.strip():
        return out2.strip()
    # 最后兜底：直接读 .git/config
    cfg = SCRIPT_DIR / '.git' / 'config'
    try:
        for line in cfg.read_text(encoding='utf-8', errors='replace').splitlines():
            line = line.strip()
            if line.lower().startswith('url') and '=' in line:
                url = line.split('=', 1)[1].strip()
                if 'github.com' in url:
                    log(f'通过 .git/config 读到 origin: {url}')
                    return url
    except Exception:
        pass
    log(f'读取 origin 失败: get-url[{code}] {err[:100]}')
    return ''


def get_current_version():
    """获取当前版本"""
    code, out, _ = run_git(['describe', '--tags', '--always'])
    if code == 0 and out:
        return out
    code, out, _ = run_git(['log', '-1', '--oneline'])
    if code == 0 and out:
        return out[:40]
    return 'unknown'


def _fetch_one(url, branch):
    """从指定 URL fetch，并刷新 origin/<branch> 跟踪引用"""
    refspec = f'+refs/heads/{branch}:refs/remotes/origin/{branch}'
    return run_git(['fetch', '--no-tags', url, refspec], timeout=180)


def fetch_remote(branch):
    """
    依次尝试：直连 -> 各 git 镜像。
    返回 (ok: bool, channel: str, err: str, remote_sha: str)
    """
    origin_url = get_origin_url()
    if not origin_url:
        return False, '', '未配置 origin 远程', ''

    channels = [('直连', origin_url)]
    for pre in GIT_MIRROR_PREFIXES:
        if not origin_url.startswith(pre):
            channels.append((pre.rstrip('/').split('//')[-1], pre + origin_url))

    last_err = ''
    for name, url in channels:
        t0 = time.time()
        code, out, err = _fetch_one(url, branch)
        dt = time.time() - t0
        if code == 0:
            code2, sha, _ = run_git(['rev-parse', f'origin/{branch}'])
            log(f'fetch 成功（通道: {name}，{dt:.0f}秒）')
            return True, name, '', (sha.strip() if code2 == 0 else '')
        last_err = err.splitlines()[-1][:120] if err.strip() else '未知错误'
        log(f'fetch 通道 [{name}] 失败（{dt:.0f}秒）: {last_err}')

    return False, '', last_err, ''


def _download(url, dest_path, timeout=120):
    """下载文件到指定路径"""
    req = urllib.request.Request(url, headers={'User-Agent': 'git/2.40'})
    with urllib.request.urlopen(req, timeout=timeout) as resp, open(dest_path, 'wb') as f:
        shutil.copyfileobj(resp, f, length=65536)


def update_via_tarball(remote_sha, branch):
    """
    git 全部失败时的压缩包兜底方案：
    下载远端源码包覆盖到仓库（保留 accounts/usage/.git 及所有本地附加文件），
    然后用 update-ref/reset 让 git 元数据与新代码一致。
    """
    origin_url = get_origin_url()
    # 从 origin URL 提取 owner/repo
    repo_part = origin_url.split('github.com/', 1)[-1]
    repo_part = repo_part.rstrip('/').removesuffix('.git')
    sha = remote_sha or branch

    candidates = []
    for pre in ARCHIVE_PREFIXES:
        if pre:
            candidates.append(f'{pre}https://github.com/{repo_part}/archive/{sha}.tar.gz')
        else:
            candidates.append(f'https://codeload.github.com/{repo_part}/tar.gz/{sha}')

    tmp_tar = None
    for url in candidates:
        try:
            log(f'压缩包通道尝试: {url[:80]}')
            fd, tmp_tar = tempfile.mkstemp(suffix='.tar.gz', prefix='wb-update-')
            os.close(fd)
            _download(url, tmp_tar)
            size = os.path.getsize(tmp_tar)
            if size < 1024:
                raise RuntimeError(f'下载文件过小（{size} 字节），可能不是有效压缩包')
            log(f'压缩包下载完成（{size/1024/1024:.1f} MB），开始解包覆盖...')
            break
        except Exception as e:
            log(f'下载失败: {str(e)[:100]}')
            if tmp_tar and os.path.exists(tmp_tar):
                os.remove(tmp_tar)
            tmp_tar = None
    if not tmp_tar:
        return False, '所有压缩包通道均失败'

    try:
        with tarfile.open(tmp_tar, 'r:gz') as tf:
            members = tf.getmembers()
            top = members[0].name.split('/')[0] if members else ''
            for m in members:
                # 跳过最外层目录本身
                parts = m.name.split('/', 1)
                if len(parts) < 2 or not parts[1]:
                    continue
                rel = parts[1]
                root_name = rel.split('/', 1)[0]
                if root_name in PROTECTED_NAMES:
                    continue
                m.name = rel
                tf.extract(m, path=str(SCRIPT_DIR))  # noqa: S202 来源为官方仓库压缩包
        log('文件覆盖完成')

        # 让 git 认为工作区已到远端版本（--mixed 只更新索引，不删除任何未跟踪文件）
        if remote_sha:
            run_git(['update-ref', f'refs/heads/{branch}', remote_sha])
            run_git(['reset', '--mixed', remote_sha], timeout=60)
        return True, ''
    except Exception as e:
        return False, f'解包失败: {e}'
    finally:
        if tmp_tar and os.path.exists(tmp_tar):
            os.remove(tmp_tar)


def check_update():
    """检查是否有更新"""
    with _lock:
        _update_status['state'] = 'checking'
        _update_status['message'] = '正在检查更新...'
        _update_status['progress'] = 10

    log('开始检查更新...')
    branch = get_branch()
    current = get_current_version()
    with _lock:
        _update_status['current_version'] = current
    log(f'当前版本: {current}（分支: {branch}）')
    with _lock:
        _update_status['progress'] = 30

    ok, channel, err, remote_sha = fetch_remote(branch)
    if not ok:
        msg = f'检查失败（直连和镜像均不可用）: {err}'
        log(msg)
        with _lock:
            _update_status['state'] = 'error'
            _update_status['message'] = msg[:120]
        return False

    with _lock:
        _update_status['progress'] = 60

    # 读引用（完整引用路径避免歧义；短重试抵消偶发锁/IO 抖动）
    local = remote = ''
    for _ in range(3):
        code, local, e1 = run_git(['rev-parse', 'HEAD'])
        code2, remote, e2 = run_git(['rev-parse', f'refs/remotes/origin/{branch}'])
        if code == 0 and code2 == 0 and local.strip() and remote.strip():
            break
        log(f'rev-parse 重试 (local={code}/{e1[:60]}, remote={code2}/{e2[:60]})')
        time.sleep(1)

    if code == 0 and code2 == 0 and local.strip() and remote.strip():
        if local.strip() == remote.strip():
            log('已是最新版本')
            with _lock:
                _update_status['state'] = 'idle'
                _update_status['message'] = f'已是最新版本（通道: {channel}）'
                _update_status['latest_version'] = current
                _update_status['progress'] = 100
            return False
        code, tag, _ = run_git(['describe', '--tags', f'refs/remotes/origin/{branch}'])
        latest = tag if code == 0 and tag.strip() else remote[:7]
        log(f'发现新版本: {latest}')
        with _lock:
            _update_status['state'] = 'idle'
            _update_status['message'] = f'发现新版本: {latest}（通道: {channel}）'
            _update_status['latest_version'] = latest
            _update_status['progress'] = 100
        return True

    with _lock:
        _update_status['state'] = 'error'
        _update_status['message'] = '无法获取版本信息'
    return False


def _wait_port(port, timeout_sec=90):
    """等待本地端口恢复可用"""
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        try:
            urllib.request.urlopen(f'http://127.0.0.1:{port}/', timeout=3).read()
            return True
        except Exception:
            time.sleep(3)
    return False


def _kill_proxy_processes():
    """
    结束代理相关进程，交给看门狗自动重启（不动看门狗/托盘/更新器自身）。
    新版 Windows 已移除 wmic，统一走 PowerShell CIM 查询并结束进程。
    """
    my_pid = os.getpid()
    killed = []
    ps_script = (
        "$me = %d; "
        "Get-CimInstance Win32_Process -Filter \"Name='python.exe' OR Name='pythonw.exe'\" | "
        "Where-Object { $_.CommandLine -match 'wb_proxy\\.py|wb_429_fixer\\.py' "
        "-and $_.ProcessId -ne $me } | "
        "ForEach-Object { try { Stop-Process -Id $_.ProcessId -Force -ErrorAction Stop; "
        "$_.ProcessId } catch {} }" % my_pid
    )
    try:
        result = subprocess.run(
            ['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', ps_script],
            capture_output=True, timeout=25,
            creationflags=subprocess.CREATE_NO_WINDOW
        )
        for line in result.stdout.decode('utf-8', 'replace').splitlines():
            line = line.strip()
            if line.isdigit():
                killed.append(int(line))
        if not killed:
            err = result.stderr.decode('utf-8', 'replace').strip()
            if err:
                log(f'PowerShell 结束进程提示: {err[:100]}')
    except Exception as e:
        log(f'结束旧进程时出错: {e}')
    return killed


def do_update():
    """执行更新"""
    def worker():
        with _lock:
            _update_status['state'] = 'updating'
            _update_status['message'] = '正在更新...'
            _update_status['progress'] = 5
            _update_status['log'] = []

        log('开始更新...')
        branch = get_branch()

        # 1. 备份数据
        with _lock:
            _update_status['progress'] = 10
        backup_dir = SCRIPT_DIR.parent / f'backup-update-{int(time.time())}'
        try:
            if (SCRIPT_DIR / 'accounts').exists():
                shutil.copytree(SCRIPT_DIR / 'accounts', backup_dir / 'accounts')
            if (SCRIPT_DIR / 'usage').exists():
                shutil.copytree(SCRIPT_DIR / 'usage', backup_dir / 'usage')
            log(f'已备份账号与用量数据到: {backup_dir.name}')
        except Exception as e:
            log(f'备份警告: {e}')

        with _lock:
            _update_status['progress'] = 25

        # 2. 多通道 fetch
        log('拉取最新代码（自动选择可用通道）...')
        ok, channel, err, remote_sha = fetch_remote(branch)

        updated = False
        if ok:
            with _lock:
                _update_status['progress'] = 45

            remote_ref = f'refs/remotes/origin/{branch}'
            code, local, _ = run_git(['rev-parse', 'HEAD'])
            code2, remote, _ = run_git(['rev-parse', remote_ref])
            if code == 0 and code2 == 0 and local.strip() == remote.strip():
                log('代码已是最新，无需合并')
                updated = True
            else:
                # 3. 快进合并（本地自定义文件均为未跟踪文件，不影响合并）
                # 先重试 3 次，排除 git.exe 偶发启动失败（如 0xC0000142）
                log('合并新代码...')
                code, out, merr = -1, '', ''
                for attempt in range(3):
                    code, out, merr = run_git(['merge', '--ff-only', remote_ref], timeout=60)
                    if code == 0:
                        break
                    log(f'合并尝试 {attempt+1}/3 失败: {(merr or "未知错误")[:100]}')
                    time.sleep(2)
                if code == 0:
                    updated = True
                    log(f'合并成功: {out[:120]}')
                else:
                    # 只暂存「已跟踪文件」的改动，绝不能带 --include-untracked，
                    # 否则会把看门狗/429代理等本地附加文件一起移走
                    log('尝试先暂存已跟踪文件的改动再合并...')
                    run_git(['stash'], timeout=60)
                    code, out, merr2 = run_git(['merge', '--ff-only', remote_ref], timeout=60)
                    if code == 0:
                        updated = True
                        log('暂存后合并成功')
                    else:
                        log(f'合并仍失败，转压缩包兜底: {(merr2 or "")[:100]}')

        # 4. git 全部走不通时，压缩包兜底
        if not updated:
            with _lock:
                _update_status['progress'] = 55
            log('git 通道不可用，启用压缩包兜底方案...')
            if not remote_sha:
                # git 没拿到 sha 时，用镜像 ls-remote 再试一次
                origin_url = get_origin_url()
                for pre in GIT_MIRROR_PREFIXES:
                    code, out, _ = run_git(['ls-remote', pre + origin_url, f'refs/heads/{branch}'], timeout=30)
                    if code == 0 and out.strip():
                        remote_sha = out.split()[0]
                        break
            tar_ok, tar_err = update_via_tarball(remote_sha, branch)
            if not tar_ok:
                with _lock:
                    _update_status['state'] = 'error'
                    _update_status['message'] = f'更新失败: {tar_err[:100]}'
                return
            updated = True
            log('压缩包兜底更新完成')

        with _lock:
            _update_status['progress'] = 75

        # 5. 重启代理（看门狗会自动拉起新代码）
        log('正在重启代理服务（看门狗将自动拉起）...')
        with _lock:
            _update_status['state'] = 'restarting'
            _update_status['message'] = '正在重启服务...'
        killed = _kill_proxy_processes()
        if killed:
            log(f'已结束旧代理进程 PID: {killed}')
        else:
            log('未发现运行中的代理进程（看门狗会处理）')

        with _lock:
            _update_status['progress'] = 85

        log('等待服务恢复（最多 90 秒）...')
        if _wait_port(MAIN_PORT, timeout_sec=90):
            log('服务已恢复')
        else:
            log('⚠️ 服务恢复超时，请稍后手动查看托盘图标')

        time.sleep(2)
        new_version = get_current_version()
        with _lock:
            _update_status['current_version'] = new_version
            _update_status['latest_version'] = new_version
            _update_status['state'] = 'success'
            _update_status['message'] = f'更新成功！当前版本: {new_version}'
            _update_status['progress'] = 100

        log(f'更新完成！新版本: {new_version}')

    threading.Thread(target=worker, daemon=True).start()


class UpdateHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/' or self.path == '/index.html':
            self._send_html()
        elif self.path == '/api/status':
            self._send_json(_update_status)
        elif self.path == '/api/check':
            threading.Thread(target=check_update, daemon=True).start()
            self._send_json({'ok': True, 'message': '开始检查'})
        elif self.path == '/api/update':
            do_update()
            self._send_json({'ok': True, 'message': '开始更新'})
        else:
            self.send_error(404)

    def do_POST(self):
        if self.path == '/api/check':
            threading.Thread(target=check_update, daemon=True).start()
            self._send_json({'ok': True, 'message': '开始检查'})
        elif self.path == '/api/update':
            do_update()
            self._send_json({'ok': True, 'message': '开始更新'})
        else:
            self.send_error(404)

    def _send_html(self):
        html = HTML_PAGE
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(html.encode('utf-8'))))
        self.end_headers()
        self.wfile.write(html.encode('utf-8'))

    def _send_json(self, data):
        body = json.dumps(data, ensure_ascii=False).encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass  # 静默日志


HTML_PAGE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>WorkBuddy Proxy - 一键更新</title>
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Microsoft YaHei', sans-serif;
    background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
    min-height: 100vh;
    display: flex;
    align-items: center;
    justify-content: center;
    padding: 20px;
  }
  .card {
    background: white;
    border-radius: 16px;
    box-shadow: 0 20px 60px rgba(0,0,0,0.3);
    width: 100%;
    max-width: 520px;
    overflow: hidden;
  }
  .header {
    background: linear-gradient(135deg, #0078d4 0%, #00bcf2 100%);
    color: white;
    padding: 28px 32px;
  }
  .header h1 { font-size: 22px; font-weight: 600; margin-bottom: 6px; }
  .header p { font-size: 13px; opacity: 0.9; }
  .content { padding: 28px 32px; }
  .version-info {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 16px;
    margin-bottom: 24px;
  }
  .ver-box {
    background: #f8f9fa;
    border-radius: 10px;
    padding: 16px;
    text-align: center;
  }
  .ver-label { font-size: 12px; color: #666; margin-bottom: 6px; }
  .ver-value { font-size: 15px; font-weight: 600; color: #333; word-break: break-all; }
  .status-box {
    background: #f0f7ff;
    border: 1px solid #d0e3ff;
    border-radius: 10px;
    padding: 16px;
    margin-bottom: 20px;
  }
  .status-text { font-size: 14px; color: #0078d4; font-weight: 500; }
  .progress-bar {
    width: 100%;
    height: 6px;
    background: #e5e7eb;
    border-radius: 3px;
    margin-top: 10px;
    overflow: hidden;
  }
  .progress-fill {
    height: 100%;
    background: linear-gradient(90deg, #0078d4, #00bcf2);
    border-radius: 3px;
    transition: width 0.3s ease;
    width: 0%;
  }
  .btn-group {
    display: flex;
    gap: 12px;
    margin-bottom: 20px;
  }
  .btn {
    flex: 1;
    padding: 12px 20px;
    border: none;
    border-radius: 8px;
    font-size: 14px;
    font-weight: 500;
    cursor: pointer;
    transition: all 0.2s;
  }
  .btn-primary {
    background: linear-gradient(135deg, #0078d4, #00bcf2);
    color: white;
  }
  .btn-primary:hover { transform: translateY(-1px); box-shadow: 0 4px 12px rgba(0,120,212,0.4); }
  .btn-primary:disabled { opacity: 0.5; cursor: not-allowed; transform: none; box-shadow: none; }
  .btn-secondary {
    background: #f3f4f6;
    color: #374151;
  }
  .btn-secondary:hover { background: #e5e7eb; }
  .btn-secondary:disabled { opacity: 0.5; cursor: not-allowed; }
  .log-box {
    background: #1e1e1e;
    color: #d4d4d4;
    border-radius: 8px;
    padding: 14px;
    font-family: 'Consolas', 'Monaco', monospace;
    font-size: 12px;
    line-height: 1.6;
    max-height: 220px;
    overflow-y: auto;
  }
  .log-line { white-space: pre-wrap; word-break: break-all; }
  .footer {
    text-align: center;
    padding: 16px 32px;
    background: #f9fafb;
    border-top: 1px solid #e5e7eb;
    font-size: 12px;
    color: #9ca3af;
  }
  .footer a { color: #0078d4; text-decoration: none; }
  .success { color: #10b981 !important; }
  .error { color: #ef4444 !important; }
  .warning { color: #f59e0b !important; }
</style>
</head>
<body>
<div class="card">
  <div class="header">
    <h1>🔄 WorkBuddy Proxy 更新中心</h1>
    <p>一键检查并更新到最新版本（国内网络自动走镜像）</p>
  </div>
  <div class="content">
    <div class="version-info">
      <div class="ver-box">
        <div class="ver-label">当前版本</div>
        <div class="ver-value" id="currentVer">--</div>
      </div>
      <div class="ver-box">
        <div class="ver-label">最新版本</div>
        <div class="ver-value" id="latestVer">--</div>
      </div>
    </div>
    <div class="status-box">
      <div class="status-text" id="statusText">准备就绪</div>
      <div class="progress-bar">
        <div class="progress-fill" id="progressFill"></div>
      </div>
    </div>
    <div class="btn-group">
      <button class="btn btn-secondary" id="checkBtn" onclick="checkUpdate()">检查更新</button>
      <button class="btn btn-primary" id="updateBtn" onclick="doUpdate()">立即更新</button>
    </div>
    <div class="log-box" id="logBox">
      <div class="log-line">等待操作...</div>
    </div>
  </div>
  <div class="footer">
    数据不会丢失 · 更新后自动重启 · <a href="http://127.0.0.1:8788/" target="_blank">返回看板</a>
  </div>
</div>
<script>
let polling = false;

function pollStatus() {
  if (polling) return;
  polling = true;
  fetch('/api/status')
    .then(r => r.json())
    .then(data => {
      document.getElementById('currentVer').textContent = data.current_version || '--';
      document.getElementById('latestVer').textContent = data.latest_version || '--';
      document.getElementById('statusText').textContent = data.message || '准备就绪';
      document.getElementById('progressFill').style.width = (data.progress || 0) + '%';

      const statusEl = document.getElementById('statusText');
      statusEl.className = 'status-text';
      if (data.state === 'success') statusEl.classList.add('success');
      if (data.state === 'error') statusEl.classList.add('error');
      if (data.state === 'checking' || data.state === 'updating' || data.state === 'restarting') {
        statusEl.classList.add('warning');
      }

      const logBox = document.getElementById('logBox');
      if (data.log && data.log.length > 0) {
        logBox.innerHTML = data.log.map(l => '<div class="log-line">' + escapeHtml(l) + '</div>').join('');
        logBox.scrollTop = logBox.scrollHeight;
      }

      const busy = data.state === 'checking' || data.state === 'updating' || data.state === 'restarting';
      document.getElementById('checkBtn').disabled = busy;
      document.getElementById('updateBtn').disabled = busy;

      if (busy) {
        setTimeout(() => { polling = false; pollStatus(); }, 1000);
      } else {
        polling = false;
      }
    })
    .catch(() => { polling = false; });
}

function checkUpdate() {
  fetch('/api/check', {method: 'POST'});
  pollStatus();
}

function doUpdate() {
  if (!confirm('确定要更新吗？更新过程中服务会短暂重启。\\n账号数据不会丢失。')) return;
  fetch('/api/update', {method: 'POST'});
  pollStatus();
}

function escapeHtml(text) {
  const div = document.createElement('div');
  div.appendChild(document.createTextNode(text));
  return div.innerHTML;
}

// 初始化
pollStatus();
</script>
</body>
</html>
"""


def main():
    # 初始化版本信息
    _update_status['current_version'] = get_current_version()
    _update_status['message'] = '准备就绪，点击检查更新'

    server = HTTPServer(('127.0.0.1', PORT), UpdateHandler)
    print(f'[wb-updater] 更新服务已启动: http://127.0.0.1:{PORT}/', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    main()
