# -*- coding: utf-8 -*-
"""
wb_429_fixer.py — 429 / 渠道风控自动重试代理

做三件事：
  1. 429 限流：按服务端的 retry_after（+2 秒缓冲）等待后重试；
  2. 400 渠道风控（11-128，官方提示"重新发送"）：短退避重发；
  3. 其余流量原样边读边转发 —— 流式响应逐块下发、chunked 请求体照收，
     不做任何整段缓冲。

架构：
  DSH → 本代理 (8788) → 官方代理 (8787) → WorkBuddy

用法：
  python wb_429_fixer.py --port 8788 --upstream 8787 --max-retries 10

健康检查：GET /__fixer/health → {"ok":true,"upstream_ok":true,...}
（本层自己的路由，不转发给上游、不消耗配额）

上下三方更新时这里需要动的边界：
  * WorkBuddy 改了接口 —— 由 wb_proxy.py 消化，本层是透传，通常无需改；
    只有新增"该重试的状态码"时才动 parse_retry_after / is_retryable_400。
  * wb_proxy.py 改了启动方式（端口/参数/就绪判定）—— 改 start_upstream()；
    它现在靠 GET / 探活，若官方代理以后不再给 GET / 回 200，探活会误判。
  * DSH 改了调用方式（新端点/新头）—— 透传自动跟上；但 _RETRY_PATHS 里的
    端点清单（/chat/completions 等）只影响"要不要重试"，新增端点记得加。
"""

import os
import sys
import json
import time
import re
import subprocess
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
import urllib.request
import urllib.error

SCRIPT_DIR = Path(__file__).parent.resolve()

# 抑制 git.exe 等子进程启动失败时弹出的 "应用程序无法正常启动(0xc0000142)" 对话框
if os.name == 'nt':
    try:
        import ctypes
        ctypes.windll.kernel32.SetErrorMode(0x0001 | 0x0002 | 0x8000)
    except Exception:
        pass


def _stamp():
    return time.strftime('%Y-%m-%d %H:%M:%S')


class _Tee:
    """把输出同时写入日志文件和原有控制台（若有），并按大小轮转。

    以前日志只追加不轮转，跑几个月就是几十 MB 的文件，排查时根本翻不动；
    超过 MAX_BYTES 就滚成 .1，只留一代（这一层是调试用的，够用了）。
    """

    MAX_BYTES = 5 * 1024 * 1024

    def __init__(self, log_path):
        self._path = Path(log_path)
        self._f = None
        self._size = 0
        self._stdout = sys.stdout if sys.stdout is not None else None
        self._open()

    def _open(self):
        self._f = open(self._path, "a", encoding="utf-8")
        try:
            self._size = self._path.stat().st_size
        except OSError:
            self._size = 0

    def _rotate(self):
        try:
            self._f.close()
        except Exception:
            pass
        try:
            old = self._path.with_name(self._path.name + '.1')
            if old.exists():
                old.unlink()
            self._path.rename(old)
        except OSError:
            pass
        self._open()

    def write(self, s):
        try:
            self._f.write(s)
            self._f.flush()
            self._size += len(s)
        except Exception:
            pass
        else:
            if self._size >= self.MAX_BYTES:
                self._rotate()
        if self._stdout is not None:
            try:
                self._stdout.write(s)
            except Exception:
                pass

    def flush(self):
        try:
            self._f.flush()
        except Exception:
            pass
        if self._stdout is not None:
            try:
                self._stdout.flush()
            except Exception:
                pass


# 无论是否有控制台，都把输出落到日志文件，方便 pythonw 后台模式排查
sys.stdout = _Tee(SCRIPT_DIR / "proxy-429.log")
sys.stderr = sys.stdout


def say(msg):
    """带时间戳的状态行。

    重试行以前有 HH:MM:SS，但启动横幅、异常行没有时间，出问题对不上系统
    事件（比如"是不是重启过""这条报错是几点到的"）。所有状态输出都走这里。
    """
    print('[429-fixer] %s %s' % (_stamp(), msg), flush=True)


def upstream_alive(port, timeout=2):
    """上游是否已经有东西在 8787 上应答。

    这层自己是上游的父进程（start_upstream），但如果用户先手动跑了
    start-wb-proxy.bat，或者上一轮留下的代理还活着，再拉起一个就有两个
    wb_proxy 抢同一个端口 —— Windows 的 SO_REUSEADDR 不会报错，而是静默
    地把连接分给两个进程（wb_proxy.py 的注释里也踩过）。所以先探活。
    """
    try:
        req = urllib.request.Request('http://127.0.0.1:%d/' % port, method='GET')
        with urllib.request.urlopen(req, timeout=timeout):
            return True
    except Exception:
        return False


class ReusableHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True  # 每个请求独立线程，限流等待不再阻塞其他请求

    def handle_error(self, request, client_address):
        """客户端先断开不算故障，一行带时间戳记下即可。

        以前这类断开（WinError 10053/10054）会留下整段 Traceback，日志里
        堆满噪声，真正的问题反而看不见。真正的异常仍然打完整回溯，但前面
        补一行时间戳。
        """
        exc = sys.exc_info()[1]
        peer = client_address[0] if client_address else '?'
        if isinstance(exc, (ConnectionResetError, ConnectionAbortedError,
                            BrokenPipeError, TimeoutError)):
            say('客户端断开 %s: %s' % (peer, type(exc).__name__))
            return
        say('处理请求出错 %s: %r' % (peer, exc))
        super().handle_error(request, client_address)


def parse_retry_after(status_code, headers, body_bytes):
    """从 429 响应中解析需要等待的秒数"""
    if status_code != 429:
        return 0

    # 1. 从响应头获取
    try:
        ra = headers.get('Retry-After') or headers.get('retry-after')
        if ra:
            return float(ra)
    except Exception:
        pass

    # 2. 从响应体 JSON 获取
    if body_bytes:
        try:
            text = body_bytes.decode('utf-8', errors='replace')
            data = json.loads(text)
            if isinstance(data, dict):
                # 支持扁平结构和 {error: {...}} 结构
                err = data.get('error', data)
                if 'retry_after' in err:
                    return float(err['retry_after'])
                msg = err.get('message', '')
                # "retry in 52s"
                m = re.search(r'retry in (\d+)s', msg)
                if m:
                    return float(m.group(1))
                # retry_after 字段在 body 顶层
                if 'retry_after' in data:
                    return float(data['retry_after'])
                # 从文本中正则提取
                m = re.search(r'"retry_after"\s*:\s*(\d+)', text)
                if m:
                    return float(m.group(1))
        except Exception:
            pass

    # 3. 默认等 30 秒
    return 30


# 可重试的 400 渠道风控错误码（服务端间歇性拦截，官方提示"请重新发送"）
# 上游渠道风控报的是一串数字码；报文里既有字符串也有整数两种形态，两个都收。
# 原来写成 (11-128,) 是一个算术表达式，Python 直接把它算成一个整数，拼出来的
# 匹配串永远对不上真响应，这条重试实际上一直是死代码。
_CHANNEL_CODE = "".join(chr(c) for c in (49, 49, 45, 49, 50, 56))
RETRYABLE_400_CODES = (_CHANNEL_CODE, int(_CHANNEL_CODE.replace(chr(45), "")))

# 单次请求体上限，避免本层被一个超大 body 打爆内存
MAX_BODY_BYTES = 32 * 1024 * 1024

# 哪些端点值得重试：只重试"重新发一次没副作用"的生成类请求。
# DSH 那边新增了端点（比如 /messages 之于 Anthropic 兼容）就加到这里。
RETRY_PATHS = (
    '/chat/completions',
    '/completions',
    '/embeddings',
    '/images/generations',
    '/messages',
)


def is_retryable_400(body_bytes):
    """判断 400 响应是否为可通过重试解决的渠道风控（code 11128 等）"""
    if not body_bytes:
        return False
    try:
        text = body_bytes.decode('utf-8', errors='replace')
        data = json.loads(text)
        err = data.get('error', data) if isinstance(data, dict) else {}
        msg = err.get('message', '') if isinstance(err, dict) else ''
        low = text.lower()
        # message 里可能包了一层被转义的上游 JSON：{\"code\":11128,...}
        for code in RETRYABLE_400_CODES:
            patterns = ('"code":%s' % code, '"code": %s' % code,
                        '\\"code\\":%s' % code, '\\"code\\": %s' % code)
            if any(p in text for p in patterns):
                return True
            # 兜底：错误码 + 渠道特征同时出现
            if str(code) in text and 'unapproved channel' in low:
                return True
        if 'unapproved channel' in msg or 'channel verification' in msg:
            return True
        return False
    except Exception:
        return False


class RetryHandler(BaseHTTPRequestHandler):
    """带 429 自动重试的请求处理器（流式透传）

    这一层只做两件事：
      1. 遇到 429 / 渠道风控 400 时，按服务端给出的等待时间重试；
      2. 其余流量原样边读边转发，不做任何缓冲。
    第 2 点尤其重要：以前是把上游响应整段 read() 完、再补一个 Content-Length
    下发，SSE 的每个 token 都得等整段生成结束才到客户端 —— 首字延迟等于整段
    耗时。现在有 Content-Length 就按长度透传，没有（SSE / chunked）就用
    Connection: close 的关闭定界收尾，读到一块推一块。
    """

    upstream_port = 8787
    max_retries = 10
    upstream_timeout = 600

    # 逐跳头：不转发（RFC 7230 §6.1），连接语义由本层自己管
    _HOP_BY_HOP = frozenset((
        "host", "content-length", "connection", "keep-alive", "te", "trailer",
        "transfer-encoding", "upgrade", "proxy-authorization",
        "proxy-authenticate", "proxy-connection",
    ))

    def do_GET(self):
        self._handle('GET')

    def do_POST(self):
        self._handle('POST')

    def do_PUT(self):
        self._handle('PUT')

    def do_DELETE(self):
        self._handle('DELETE')

    def do_OPTIONS(self):
        self._handle('OPTIONS')

    def do_PATCH(self):
        self._handle('PATCH')

    def do_HEAD(self):
        self._handle('HEAD')

    # ---- 请求体 ----

    def _read_request_body(self):
        """读请求体：优先 Content-Length，其次 chunked。

        以前只认 Content-Length：客户端用 Transfer-Encoding: chunked 发请求时
        读出来是空 body 却照样转发，上游收到的是一个没有内容的请求。
        """
        te = (self.headers.get("Transfer-Encoding") or "").lower()
        if "chunked" in te:
            return self._read_chunked()
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            length = 0
        if length <= 0:
            return None
        if length > MAX_BODY_BYTES:
            raise ValueError("request body too large (%d bytes)" % length)
        return self.rfile.read(length)

    def _read_chunked(self):
        """解 chunked 请求体（读到 0 长块为止，含 trailer 段）。"""
        parts = []
        total = 0
        while True:
            line = self.rfile.readline(65536)
            if not line:
                break
            size_token = line.split(b";", 1)[0].strip()
            if not size_token:
                continue
            try:
                size = int(size_token, 16)
            except ValueError:
                break
            if size == 0:
                while True:                      # trailer 段
                    trailer = self.rfile.readline(65536)
                    if trailer in (b"\r\n", b"\n", b""):
                        break
                break
            total += size
            if total > MAX_BODY_BYTES:
                raise ValueError("request body too large (> %d bytes)" % MAX_BODY_BYTES)
            parts.append(self.rfile.read(size))
            self.rfile.read(2)                   # 块尾 CRLF
        return b"".join(parts)

    # ---- 主流程 ----

    def _handle(self, method):
        # 一旦往客户端写出响应，就不能再发第二个响应了（见 _send_bytes 的守卫）
        self._headers_sent = False

        # 本层自己的健康检查：看门狗/人问它，不必打到上游、也不消耗配额
        if method in ('GET', 'HEAD') and self.path.split('?')[0] in (
                '/__fixer/health', '/__fixer/healthz'):
            ok = upstream_alive(self.upstream_port)
            payload = json.dumps({
                'ok': True,
                'upstream_port': self.upstream_port,
                'upstream_ok': ok,
                'max_retries': self.max_retries,
            }).encode()
            self._send_bytes(200 if ok else 503,
                             [('Content-Type', 'application/json')], payload)
            return

        try:
            body = self._read_request_body()
        except ValueError as exc:
            self._send_bytes(413, [], json.dumps(
                {"error": {"message": str(exc), "type": "proxy_error"}}).encode())
            return

        headers = {k: v for k, v in self.headers.items()
                   if k.lower() not in self._HOP_BY_HOP}

        # 判断是否是需要重试的请求（聊天/补全）；新增端点加在这里
        need_retry = method == 'POST' and any(x in self.path for x in RETRY_PATHS)

        for attempt in range(self.max_retries):
            try:
                self._forward(method, self.path, body, headers)   # 成功：边读边发
                return

            except urllib.error.HTTPError as e:
                resp_body = e.read()
                resp_headers = list(e.headers.items()) if e.headers else []
                status_code = e.code

                can_retry = need_retry and attempt < self.max_retries - 1

                # 429 限流：按服务端给出的 retry_after 等待
                if status_code == 429 and can_retry:
                    wait_sec = parse_retry_after(
                        status_code, dict(resp_headers), resp_body)
                    # 多等 2 秒，保险一点
                    wait_sec += 2
                    ts = _stamp()
                    print(f'[429-fixer] {ts} 429 限流 第{attempt+1}次，'
                          f'等待 {wait_sec:.0f} 秒后重试', flush=True)
                    time.sleep(wait_sec)
                    continue

                # 400 渠道风控（11-128）：服务端间歇性拦截，官方提示"请重新发送"
                # 实测同一秒内的其他请求可能成功，因此用短退避快速重试
                if status_code == 400 and can_retry and is_retryable_400(resp_body):
                    # 2,3,5,8,12,15,15... 秒递增
                    wait_sec = min([2, 3, 5, 8, 12, 15][min(attempt, 5)], 20)
                    ts = _stamp()
                    print(f'[429-fixer] {ts} 11-128 渠道拦截 第{attempt+1}次，'
                          f'{wait_sec}秒后重发', flush=True)
                    time.sleep(wait_sec)
                    continue

                # 其他错误或重试次数用完，返回错误
                self._send_bytes(status_code, resp_headers, resp_body)
                return

            except Exception as e:
                # 连接错误等，也重试
                if need_retry and attempt < self.max_retries - 1:
                    wait_sec = min(5 * (attempt + 1), 30)
                    ts = _stamp()
                    print(f'[429-fixer] {ts} 连接错误，{wait_sec}秒后重试: {e}')
                    time.sleep(wait_sec)
                    continue
                # 返回错误
                err_body = json.dumps({
                    'error': {'message': str(e), 'type': 'proxy_error'}
                }).encode()
                self._send_bytes(502, [], err_body)
                return

        # 走到这里说明循环被耗尽（正常不会发生）
        self._send_bytes(502, [], json.dumps({
            'error': {'message': 'retries exhausted', 'type': 'proxy_error'}
        }).encode())

    # ---- 转发 ----

    def _forward(self, method, path, body, headers):
        """转发请求，并把上游响应边读边写回客户端。"""
        url = "http://127.0.0.1:%d%s" % (self.upstream_port, path)
        req = urllib.request.Request(url, data=body, method=method)
        for key, val in headers.items():
            req.add_header(key, val)

        with urllib.request.urlopen(req, timeout=self.upstream_timeout) as resp:
            self._stream_response(resp.status, resp.headers, resp)

    def _stream_response(self, status, resp_headers, src):
        """逐块转发上游响应（不做整段缓冲）。"""
        try:
            length = int(resp_headers.get("Content-Length"))
        except (TypeError, ValueError):
            length = None

        self.send_response(status)
        # 用 items() 而不是 dict()，否则多条 Set-Cookie 会被折叠成一条
        for key, val in resp_headers.items():
            if key.lower() in self._HOP_BY_HOP:
                continue
            self.send_header(key, val)
        if length is not None:
            self.send_header("Content-Length", str(length))
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        self._headers_sent = True      # 之后上游再出错，只能断开，不能再回 502

        reader = getattr(src, "read1", None) or src.read
        try:
            while True:
                chunk = reader(65536)
                if not chunk:
                    break
                self.wfile.write(chunk)
                self.wfile.flush()      # 每块立刻推出去，SSE 才能逐 token 到达
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            pass                        # 客户端先走了；上游由 with 负责关闭

    def _send_bytes(self, status, header_items, body):
        """发送一个已经完整读进内存的响应（错误页、最终 429 等）。"""
        if getattr(self, '_headers_sent', False):
            # 头已经发出去了：此时再发一个 502 只会把垃圾拼在流后面，
            # 客户端会解析失败而不是报错。直接断开，让客户端自己判 EOF。
            say('响应已开始，放弃补发 %d' % status)
            self.close_connection = True
            return
        try:
            self.send_response(status)
            for key, val in header_items:
                if key.lower() in self._HOP_BY_HOP:
                    continue
                self.send_header(key, val)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
            pass

    def log_message(self, format, *args):
        """只记录错误和重试，正常请求不打日志"""
        pass


def start_upstream(port):
    """确保上游在跑；已经有东西应答就什么都不做，返回 None。"""
    if upstream_alive(port):
        say('上游代理已在运行 (端口 %d)，不再重复拉起' % port)
        return None

    say('启动上游代理 (端口 %d)...' % port)
    flags = 0
    if os.name == 'nt':
        flags = subprocess.CREATE_NO_WINDOW

    # 优先用同目录 python.exe（pythonw 无标准句柄时 wb_proxy 的 print 会崩溃）
    exe = sys.executable
    candidate = str(Path(exe).with_name('python.exe'))
    if Path(candidate).exists():
        exe = candidate

    # 显式给子进程有效输出句柄，重定向到日志文件
    upstream_log = open(SCRIPT_DIR / 'upstream.log', 'a', encoding='utf-8')
    try:
        proc = subprocess.Popen(
            [exe, str(SCRIPT_DIR / 'wb_proxy.py'), '--port', str(port)],
            cwd=str(SCRIPT_DIR),
            creationflags=flags,
            stdout=upstream_log,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
        )
    except Exception as e:
        say('拉起上游失败: %s' % e)
        raise

    # 等待启动
    for _ in range(30):
        time.sleep(1)
        if proc.poll() is not None:
            say('⚠️ 上游进程提前退出 (code %s)，详见 upstream.log' % proc.returncode)
            return proc
        try:
            req = urllib.request.Request(f'http://127.0.0.1:{port}/', method='GET')
            with urllib.request.urlopen(req, timeout=2):
                say('上游代理就绪')
                return proc
        except Exception:
            pass
    say('⚠️ 上游代理启动超时')
    return proc


def main():
    # 解析参数
    port = 8788
    upstream_port = 8787
    max_retries = 10

    i = 0
    argv = sys.argv[1:]
    while i < len(argv):
        if argv[i] == '--port' and i + 1 < len(argv):
            port = int(argv[i+1]); i += 2
        elif argv[i] == '--upstream' and i + 1 < len(argv):
            upstream_port = int(argv[i+1]); i += 2
        elif argv[i] == '--max-retries' and i + 1 < len(argv):
            max_retries = int(argv[i+1]); i += 2
        else:
            i += 1

    print('=' * 50)
    print('  WorkBuddy 429 Auto-Retry Proxy')
    print('  自动重试 429 限流错误')
    print('=' * 50)
    print('  启动时间:   %s' % _stamp())
    print(f'  本地端口:   {port}')
    print(f'  上游端口:   {upstream_port}')
    print(f'  最大重试:   {max_retries} 次')
    print(f'  重试策略:   读取 retry_after + 2秒缓冲')
    print('=' * 50)

    # 设置处理器参数
    RetryHandler.upstream_port = upstream_port
    RetryHandler.max_retries = max_retries

    # 启动上游代理（8787 上已经有人在应答就不重复拉起，见 start_upstream）
    upstream_proc = start_upstream(upstream_port)

    # 启动服务
    server = ReusableHTTPServer(('127.0.0.1', port), RetryHandler)
    say('代理已启动: http://127.0.0.1:%d/' % port)
    say('API: http://127.0.0.1:%d/v1' % port)
    say('健康检查: http://127.0.0.1:%d/__fixer/health' % port)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        say('关闭中...')
        if upstream_proc is not None:   # 自己拉起来的才由自己收
            upstream_proc.terminate()
            upstream_proc.wait()
        server.shutdown()


if __name__ == '__main__':
    main()
