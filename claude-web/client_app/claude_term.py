#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
claude-term - 바깥 PC 에서 진짜 PuTTY 로 사내 서버에 붙는 클라이언트
=====================================================================

사용자의 PC(사내망 밖)에서 돈다. VDI 에서 도는 중계(relay.exe)와는 다른
프로그램이다.

    PuTTY ─ 127.0.0.1 ─ claude-term ═ 챗봇 서버 ═ 내 VDI 중계 ─ 대상:22

이 프로그램이 하는 일은 넷이다.

    1. 서버 고르기   웹에서 관리자가 등록한 서버 중 내가 쓸 수 있는 것만 보인다
    2. 터널 쥐기     127.0.0.1 에 포트를 하나 열고 바이트를 챗봇 서버로 옮긴다
    3. 탭 안의 PuTTY 그 포트로 붙는 putty.exe 를 띄워 **탭 안에 넣는다**
                     (Win32 SetParent. MTPuTTY · SuperPuTTY 와 같은 방법).
                     넣지 못하면 별도 창으로 둔다. 터미널은 PuTTY 가 그린다
    4. Claude 패널   하나의 패널이 「대상」 으로 서버를 고른다. 대화는 서버마다
                     하나이고 웹과 같은 대화다. claude -p 는 챗봇 서버에서 돈다

이 프로그램이 하지 않는 일

    - 대상 서버의 비밀번호를 묻거나 저장하지 않는다. 탭 안의 PuTTY 에 직접 친다
    - SSH 를 하지 않는다. 바이트만 옮긴다(암호문이라 읽을 수도 없다)
    - Claude 가 PuTTY 에 대신 타이핑하지 않는다. 명령은 클립보드까지만 간다
    - 0.0.0.0 에 포트를 열지 않는다. 127.0.0.1 만, 연결 하나만 받는다
    - PuTTY 화면을 몰래 읽지 않는다. 「이 화면을 Claude 에게」 를 누를 때만
      PuTTY 의 「Copy All to Clipboard」 로 글자를 받고, 클립보드는 되돌린다.
      세션 로그 파일을 켜지 않는다

의존성
------
표준 라이브러리만 쓴다 (tkinter, ctypes, urllib, http.client, socket, threading).
putty.exe 는 이 프로그램과 **같은 폴더**에 둔다.

    pyinstaller --onefile --windowed --name claude-term claude_term.py
"""

import base64
import http.client
import json
import os
import queue
import re
import socket
import ssl
import subprocess
import sys
import threading
import time
import types
import urllib.error
import urllib.parse
import urllib.request

VERSION = "1.1.2"
APP_NAME = "Claude 터미널"

APP_DIR = os.path.join(
    os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "claude-term")
CONFIG_PATH = os.path.join(APP_DIR, "client.json")
LOG_PATH = os.path.join(APP_DIR, "claude-term.log")
LOG_MAX_BYTES = 512 * 1024

HTTP_TIMEOUT = 30
CHAT_TIMEOUT = 330          # 서버 쪽 채팅 예산(240초)보다 넉넉하게
REFRESH_SECONDS = 10
ACCEPT_SECONDS = 60         # PuTTY 가 붙기를 기다리는 시간
EMBED_SECONDS = 10          # PuTTY 창이 뜨기를 기다리는 시간. 넘으면 별도 창으로 둔다

# 「이 화면을 Claude 에게」 가 붙이는 양. 서버의 질문 길이 상한(MAX_INPUT_CHARS,
# 기본 8000자) 안에 질문까지 들어가야 하므로 글자 수도 자른다.
SCREEN_LINES = 60
SCREEN_MAX_CHARS = 6000


# ---------------------------------------------------------------------------
# 기록 (claude-term.log)
#
# 창 프로그램(--windowed)은 콘솔이 없어서, 죽어도 이유가 어디에도 남지 않는다.
# 그래서 시작 · 탭 · 끊김 · 예외 · 네이티브 충돌(faulthandler)을 파일에 남긴다.
# 키 · 비밀번호 · 화면 글자 · 질문 내용은 쓰지 않는다.
# ---------------------------------------------------------------------------
_log_file = None
_log_lock = threading.Lock()


def log(msg):
    with _log_lock:
        f = _log_file
        if f is None:
            return
        try:
            f.write("%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg))
            f.flush()
        except (OSError, ValueError):
            pass


def start_log():
    """기록을 연다. 커지면 한 벌만 .old 로 남긴다."""
    global _log_file
    try:
        os.makedirs(APP_DIR, exist_ok=True)
        if os.path.exists(LOG_PATH) and os.path.getsize(LOG_PATH) > LOG_MAX_BYTES:
            old = LOG_PATH + ".old"
            if os.path.exists(old):
                os.remove(old)
            os.rename(LOG_PATH, old)
        _log_file = open(LOG_PATH, "a", encoding="utf-8")
    except OSError:
        _log_file = None
        return
    import faulthandler
    import platform
    try:
        # 파이썬 밖(Win32 · Tk)에서 죽어도 마지막 스택이 남는다
        faulthandler.enable(file=_log_file, all_threads=True)
    except (RuntimeError, ValueError, OSError):
        pass

    def on_error(kind, exc_type, exc, tb):
        import traceback
        log("%s: %s" % (kind, "".join(
            traceback.format_exception(exc_type, exc, tb)).rstrip()))

    sys.excepthook = lambda t, e, tb: on_error("예외", t, e, tb)
    threading.excepthook = lambda a: on_error(
        "스레드 %s 예외" % getattr(a.thread, "name", "?"),
        a.exc_type, a.exc_value, a.exc_traceback)
    log("---- 시작 %s %s · Python %s · %s · %s" % (
        APP_NAME, VERSION, platform.python_version(), platform.platform(),
        sys.executable))


# ---------------------------------------------------------------------------
# 설정
# ---------------------------------------------------------------------------
def load_config():
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (IOError, OSError, ValueError):
        return None


def save_config(cfg):
    os.makedirs(APP_DIR, exist_ok=True)
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    if os.path.exists(CONFIG_PATH):
        os.remove(CONFIG_PATH)
    os.rename(tmp, CONFIG_PATH)
    _lock_down(CONFIG_PATH)


def _lock_down(path):
    """이 PC 의 다른 사용자가 키를 읽지 못하게 한다. (relay.exe 와 같은 방법)"""
    if os.name != "nt":
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        return
    user = os.environ.get("USERNAME") or ""
    if not user:
        return
    try:
        subprocess.run(["icacls", path, "/inheritance:r", "/grant:r", "%s:F" % user],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       check=False, creationflags=_no_window())
    except OSError:
        pass


def _no_window():
    return getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0


def here():
    """이 프로그램이 있는 폴더. 작업 디렉터리가 아니다(바로가기로 띄우면 다르다)."""
    target = sys.executable if getattr(sys, "frozen", False) else __file__
    return os.path.dirname(os.path.abspath(target))


def putty_path(cfg=None):
    """putty.exe 는 **이 프로그램과 같은 폴더**에서만 찾는다. 하위 폴더는 보지 않는다."""
    if cfg and cfg.get("putty_path") and os.path.exists(cfg["putty_path"]):
        return cfg["putty_path"]
    guess = os.path.join(here(), "putty.exe")
    return guess if os.path.exists(guess) else None


# ---------------------------------------------------------------------------
# 챗봇 서버 API
# ---------------------------------------------------------------------------
class ApiError(Exception):
    def __init__(self, status, message):
        super(ApiError, self).__init__(message)
        self.status = status
        self.message = message


class Api(object):
    def __init__(self, url, key=None, insecure=False):
        self.url = url.rstrip("/")
        self.key = key
        self.insecure = insecure
        self.ctx = None
        if self.url.startswith("https") and insecure:
            self.ctx = ssl.create_default_context()
            self.ctx.check_hostname = False
            self.ctx.verify_mode = ssl.CERT_NONE

    def headers(self):
        h = {"User-Agent": "claude-term/%s" % VERSION, "X-Client-Version": VERSION}
        if self.key:
            h["X-Client-Key"] = self.key
        return h

    def call(self, method, path, payload=None, timeout=HTTP_TIMEOUT):
        body = None
        req = urllib.request.Request(self.url + path, method=method)
        for k, v in self.headers().items():
            req.add_header(k, v)
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, data=body, timeout=timeout,
                                        context=self.ctx) as res:
                return json.loads(res.read().decode("utf-8") or "{}")
        except urllib.error.HTTPError as exc:
            try:
                data = json.loads(exc.read().decode("utf-8") or "{}")
            except ValueError:
                data = {}
            raise ApiError(exc.code, data.get("error") or "HTTP %d" % exc.code)
        except (urllib.error.URLError, OSError) as exc:
            raise ApiError(0, "챗봇 서버에 닿지 못했습니다. 주소와 네트워크를 확인해 "
                              "주세요. 사내망 밖에서는 VPN 이 필요할 수 있습니다. (%s)"
                           % getattr(exc, "reason", exc))


# ---------------------------------------------------------------------------
# 터널용 HTTP
#
# 중계(relay.exe)에도 같은 모양이 있다. 두 프로그램 모두 파일 하나로 묶여 따로
# 나가므로 서로 import 하지 않는다. 규약을 바꾸면 둘 다 고친다.
# ---------------------------------------------------------------------------
class Link(object):
    def __init__(self, base_url, headers, insecure=False):
        u = urllib.parse.urlsplit(base_url.rstrip("/"))
        self.https = u.scheme == "https"
        self.host = u.hostname
        self.port = u.port or (443 if self.https else 80)
        self.base = u.path.rstrip("/")
        self.headers = dict(headers)
        self.ctx = None
        if self.https:
            self.ctx = ssl.create_default_context()
            if insecure:
                self.ctx.check_hostname = False
                self.ctx.verify_mode = ssl.CERT_NONE
        self._up = None
        self._stream = None

    def _new(self, timeout):
        if self.https:
            return http.client.HTTPSConnection(self.host, self.port, timeout=timeout,
                                               context=self.ctx)
        return http.client.HTTPConnection(self.host, self.port, timeout=timeout)

    def post(self, path, payload, timeout=30):
        body = json.dumps(payload).encode("utf-8")
        hdr = dict(self.headers)
        hdr["Content-Type"] = "application/json"
        for attempt in (1, 2):
            if self._up is None:
                self._up = self._new(timeout)
            try:
                self._up.request("POST", self.base + path, body=body, headers=hdr)
                res = self._up.getresponse()
                raw = res.read()
                try:
                    data = json.loads(raw.decode("utf-8") or "{}")
                except ValueError:
                    data = {}
                return res.status, data
            except (OSError, http.client.HTTPException):
                try:
                    self._up.close()
                except OSError:
                    pass
                self._up = None
                if attempt == 2:
                    raise
        return 0, {}

    def stream(self, path, timeout=90):
        conn = self._new(timeout)
        self._stream = conn
        conn.request("GET", self.base + path, headers=self.headers)
        res = conn.getresponse()
        if res.status != 200:
            raw = res.read()
            conn.close()
            try:
                return res.status, json.loads(raw.decode("utf-8") or "{}")
            except ValueError:
                return res.status, {}

        def frames():
            try:
                while True:
                    line = res.readline()
                    if not line:
                        return
                    line = line.rstrip(b"\n")
                    kind, _, rest = line.partition(b" ")
                    if kind == b"D":
                        yield "D", base64.b64decode(rest)
                    elif kind == b"C":
                        try:
                            yield "C", json.loads(rest.decode("utf-8"))
                        except ValueError:
                            yield "C", ""
                        return
                    elif kind:
                        yield kind.decode("ascii", "replace"), None
            finally:
                conn.close()

        return 200, frames()

    def close(self):
        if self._up is not None:
            try:
                self._up.close()
            except OSError:
                pass
            self._up = None
        conn, self._stream = self._stream, None
        if conn is not None:
            # 다른 스레드가 이 연결에서 readline 으로 기다리고 있다. 윈도우에서는
            # close 만으로 그 대기가 풀리지 않는다. shutdown 이 풀어 준다.
            try:
                conn.sock.shutdown(socket.SHUT_RDWR)
            except (OSError, AttributeError):
                pass
            try:
                conn.close()
            except OSError:
                pass


def send_chunks(link, path, seq_box, data, stop):
    """조각 하나를 올린다. 실패하면 같은 순번으로 다시 보낸다(서버가 중복을 버린다)."""
    seq_box[0] += 1
    payload = {"seq": seq_box[0], "data": base64.b64encode(data).decode("ascii")}
    delay = 0.2
    for _ in range(20):
        if stop.is_set():
            return False
        try:
            status, _res = link.post(path, payload)
        except (OSError, http.client.HTTPException):
            time.sleep(delay)
            delay = min(2.0, delay * 2)
            continue
        if status == 200:
            return True
        if status == 503:
            time.sleep(0.2)
            continue
        return False
    return False


# ---------------------------------------------------------------------------
# 로컬 연결 확인
# ---------------------------------------------------------------------------
def owner_pid(local_port, peer_port):
    """
    127.0.0.1:peer_port → 127.0.0.1:local_port 연결을 연 프로세스의 PID.

    내 PC 의 다른 프로그램이 PuTTY 보다 먼저 이 포트에 붙으면, 그 프로그램이
    사내 서버의 sshd 에 날것으로 닿는다. 그래서 붙은 것이 **내가 띄운 putty.exe**
    인지 확인한다. 모르면 None (확인할 수 없는 PC 에서도 쓸 수는 있어야 한다).
    """
    if os.name != "nt":
        return None
    try:
        out = subprocess.run(["netstat", "-ano", "-p", "TCP"], capture_output=True,
                             timeout=5, creationflags=_no_window()).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    text = out.decode("mbcs" if os.name == "nt" else "utf-8", "replace")
    mine = "127.0.0.1:%d" % peer_port
    theirs = "127.0.0.1:%d" % local_port
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[1] == mine and parts[2] == theirs:
            try:
                return int(parts[-1])
            except ValueError:
                return None
    return None


def local_port_for(server_id):
    """
    서버마다 정해진 로컬 포트. PuTTY 는 호스트 키를 "호스트:포트" 로 기억하므로
    포트가 매번 바뀌면 열 때마다 호스트 키를 묻는다. 비어 있지 않으면 아무 포트나 쓴다.
    """
    return 40000 + (int(server_id) % 20000)



# ---------------------------------------------------------------------------
# 「이 화면을 Claude 에게」 가 붙이는 글자
# ---------------------------------------------------------------------------
def screen_tail(text, lines=SCREEN_LINES, max_chars=SCREEN_MAX_CHARS):
    """
    PuTTY 의 Copy All 결과에서 마지막 화면만 남긴다. (글자, 줄 수)

    Copy All 은 스크롤백 전체를 준다. 제어문자를 지우고, 커서 아래의 빈 줄을
    버리고, 마지막 lines 줄만 남긴다. 그래도 길면 위에서부터 줄을 뺀다.
    """
    rows = []
    for raw in (text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        clean = "".join(ch for ch in raw
                        if ch == "\t" or (ord(ch) >= 32 and not 127 <= ord(ch) < 160))
        rows.append(clean.rstrip())
    while rows and not rows[-1]:
        rows.pop()
    rows = rows[-lines:]
    while rows and not rows[0]:
        rows.pop(0)
    while rows and len("\n".join(rows)) > max_chars:
        rows.pop(0)
    return "\n".join(rows), len(rows)


SCREEN_HEAD = "[%s 화면 · %s · 마지막 %d줄]"
_SCREEN_RE = re.compile(r"^\[(.+?) 화면 · (.+?) · 마지막 (\d+)줄\]\n```\n.*?\n```\n\n", re.S)


def compose_question(question, shot=None):
    """
    질문 앞에 화면을 붙인다. 언어 표시 없는 블록으로 감싸므로 챗봇은 이것을
    명령으로 실행하지 않는다. 화면 안의 ``` 는 블록을 깨므로 바꿔 둔다.
    """
    if not shot:
        return question
    body = shot["text"].replace("```", "'''")
    return "%s\n```\n%s\n```\n\n%s" % (
        SCREEN_HEAD % (shot["name"], shot["at"], shot["lines"]), body, question)


def split_screen(content):
    """compose_question 의 반대. 대화에 다시 그릴 때 화면 전체 대신 칩만 보인다."""
    m = _SCREEN_RE.match(content or "")
    if not m:
        return None, content
    return ({"name": m.group(1), "at": m.group(2), "lines": int(m.group(3))},
            content[m.end():])


# ---------------------------------------------------------------------------
# Win32 — PuTTY 를 탭 안에 넣기, Copy All, 클립보드
#
# PuTTY 0.85 에서 확인한 것:
#   - 창 클래스 이름은 "PuTTY". 띄울 때 SW_HIDE 를 주면 숨긴 채로 뜬다
#   - 제목줄 스타일을 지워도 PuTTY 가 다시 붙인다. 그래서 지우지 않고, 창을
#     제목줄 높이만큼 위로 올려 탭 칸이 잘라 내게 둔다
#   - 다른 프로세스의 창을 자식으로 넣으면 클릭해도 키보드가 오지 않는다.
#     PuTTY 가 마우스를 잡는 순간(EVENT_SYSTEM_CAPTURESTART)에 포커스를 준다
#   - 시스템 메뉴의 「Copy All to Clipboard」 는 0x170 이다. 메뉴 글자로 먼저 찾는다
# ---------------------------------------------------------------------------
IDM_COPYALL = 0x0170
_W = None


def w32():
    """윈도우가 아니면 None. 그때는 PuTTY 를 별도 창으로만 띄운다."""
    global _W
    if _W is not None or os.name != "nt":
        return _W
    import ctypes
    from ctypes import wintypes as wt
    u = ctypes.WinDLL("user32", use_last_error=True)
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    H = wt.HWND
    for name, res, args in (
            ("GetWindowLongPtrW", ctypes.c_ssize_t, [H, ctypes.c_int]),
            ("SetWindowLongPtrW", ctypes.c_ssize_t, [H, ctypes.c_int, ctypes.c_ssize_t]),
            ("SetParent", H, [H, H]),
            ("GetParent", H, [H]),
            ("SetWindowPos", wt.BOOL, [H, H, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                       ctypes.c_int, wt.UINT]),
            ("ShowWindow", wt.BOOL, [H, ctypes.c_int]),
            ("RedrawWindow", wt.BOOL, [H, ctypes.c_void_p, ctypes.c_void_p, wt.UINT]),
            ("IsWindow", wt.BOOL, [H]),
            ("SetFocus", H, [H]),
            ("GetFocus", H, []),
            ("GetAncestor", H, [H, wt.UINT]),
            ("SetForegroundWindow", wt.BOOL, [H]),
            ("GetWindowRect", wt.BOOL, [H, ctypes.c_void_p]),
            ("ClientToScreen", wt.BOOL, [H, ctypes.c_void_p]),
            ("GetClassNameW", ctypes.c_int, [H, wt.LPWSTR, ctypes.c_int]),
            ("GetWindowThreadProcessId", wt.DWORD, [H, ctypes.POINTER(wt.DWORD)]),
            ("GetSystemMenu", wt.HMENU, [H, wt.BOOL]),
            ("GetMenuItemCount", ctypes.c_int, [wt.HMENU]),
            ("GetMenuItemID", wt.UINT, [wt.HMENU, ctypes.c_int]),
            ("GetMenuStringW", ctypes.c_int, [wt.HMENU, wt.UINT, wt.LPWSTR, ctypes.c_int,
                                              wt.UINT]),
            ("SendMessageTimeoutW", ctypes.c_ssize_t,
             [H, wt.UINT, wt.WPARAM, wt.LPARAM, wt.UINT, wt.UINT,
              ctypes.POINTER(ctypes.c_size_t)]),
            ("OpenClipboard", wt.BOOL, [H]),
            ("CloseClipboard", wt.BOOL, []),
            ("EmptyClipboard", wt.BOOL, []),
            ("GetClipboardData", wt.HANDLE, [wt.UINT]),
            ("SetClipboardData", wt.HANDLE, [wt.UINT, wt.HANDLE]),
            ("IsClipboardFormatAvailable", wt.BOOL, [wt.UINT]),
            ("CountClipboardFormats", ctypes.c_int, []),
            ("GetClipboardSequenceNumber", wt.DWORD, [])):
        f = getattr(u, name)
        f.restype = res
        f.argtypes = args
    k.GlobalAlloc.restype = wt.HANDLE
    k.GlobalAlloc.argtypes = [wt.UINT, ctypes.c_size_t]
    k.GlobalLock.restype = ctypes.c_void_p
    k.GlobalLock.argtypes = [wt.HANDLE]
    k.GlobalUnlock.argtypes = [wt.HANDLE]
    WinEventProc = ctypes.WINFUNCTYPE(None, wt.HANDLE, wt.DWORD, H, ctypes.c_long,
                                      ctypes.c_long, wt.DWORD, wt.DWORD)
    u.SetWinEventHook.restype = wt.HANDLE
    u.SetWinEventHook.argtypes = [wt.DWORD, wt.DWORD, wt.HMODULE, WinEventProc,
                                  wt.DWORD, wt.DWORD, wt.DWORD]
    u.UnhookWinEvent.argtypes = [wt.HANDLE]

    class RECT(ctypes.Structure):
        _fields_ = [("l", ctypes.c_long), ("t", ctypes.c_long),
                    ("r", ctypes.c_long), ("b", ctypes.c_long)]

    class POINT(ctypes.Structure):
        _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

    _W = types.SimpleNamespace(
        ctypes=ctypes, wt=wt, u=u, k=k, RECT=RECT, POINT=POINT,
        EnumProc=ctypes.WINFUNCTYPE(wt.BOOL, H, wt.LPARAM), WinEventProc=WinEventProc)
    return _W


def dpi_aware():
    """
    화면 배율(125% 등)에서 PuTTY 는 스스로 배율을 안다. 이 프로그램이 모르면
    윈도우가 이 창만 늘려 그려서, 그 안에 넣은 PuTTY 의 자리가 어긋난다.
    """
    if os.name != "nt":
        return
    import ctypes
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except (AttributeError, OSError):
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except (AttributeError, OSError):
            pass


def find_putty_window(pid):
    """그 PID 의 PuTTY 주 창. 숨겨 띄웠으므로 보이는지는 따지지 않는다."""
    w = w32()
    if w is None:
        return None
    found = []

    def cb(hwnd, _l):
        p = w.wt.DWORD()
        w.u.GetWindowThreadProcessId(hwnd, w.ctypes.byref(p))
        if p.value == pid:
            buf = w.ctypes.create_unicode_buffer(64)
            w.u.GetClassNameW(hwnd, buf, 64)
            if buf.value == "PuTTY":
                found.append(hwnd)
                return False
        return True

    w.u.EnumWindows(w.EnumProc(cb), 0)
    return found[0] if found else None


def find_putty_dialogs(pid):
    """
    그 PuTTY 가 띄운 대화상자(호스트 키 확인 「PuTTY Security Alert」, 오류 창 등).

    PuTTY 를 탭 안에 넣으면 이 창들의 주인이 이 프로그램의 창이 되어, 뒤에 깔려
    안 보일 수 있다. 그동안 PuTTY 는 대답을 기다리며 탭을 그리지 않는다.
    """
    w = w32()
    if w is None or not pid:
        return []
    found = []

    def cb(hwnd, _l):
        p = w.wt.DWORD()
        w.u.GetWindowThreadProcessId(hwnd, w.ctypes.byref(p))
        if p.value == pid and w.u.IsWindowVisible(hwnd):
            buf = w.ctypes.create_unicode_buffer(64)
            w.u.GetClassNameW(hwnd, buf, 64)
            # 0.85 의 호스트 키 창은 "PuTTYHostKeyDialog", 오류 창은 "#32770" 이다.
            # 이름에 기대지 않고, 보이는 창 중 터미널("PuTTY")이 아닌 것을 모두 잡는다.
            if buf.value != "PuTTY":
                found.append(hwnd)
        return True

    w.u.EnumWindows(w.EnumProc(cb), 0)
    return found


def bring_dialog(hwnd, over):
    """대화상자를 over(이 프로그램 창) 가운데로 옮기고 맨 앞으로."""
    w = w32()
    if w is None or not hwnd or not w.u.IsWindow(hwnd):
        return
    d, o = w.RECT(), w.RECT()
    w.u.GetWindowRect(hwnd, w.ctypes.byref(d))
    w.u.GetWindowRect(over, w.ctypes.byref(o))
    x = o.l + max(0, ((o.r - o.l) - (d.r - d.l)) // 2)
    y = o.t + max(0, ((o.b - o.t) - (d.b - d.t)) // 3)
    # 잠깐 TOPMOST 로 올렸다가 내린다. 계속 TOPMOST 로 두면 다른 프로그램 위에 남는다.
    w.u.SetWindowPos(hwnd, -1, x, y, 0, 0, 0x0001 | 0x0040)      # NOSIZE|SHOWWINDOW
    w.u.SetWindowPos(hwnd, -2, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0040)
    w.u.SetForegroundWindow(hwnd)


def embed_window(hwnd, parent):
    """PuTTY 창을 parent(탭 칸)의 자식으로 넣는다. 실패하면 False."""
    w = w32()
    if w is None or not w.u.IsWindow(hwnd):
        return False
    style = w.u.GetWindowLongPtrW(hwnd, -16)                      # GWL_STYLE
    style = (style & ~0x80000000) | 0x40000000                    # -WS_POPUP +WS_CHILD
    w.u.SetWindowLongPtrW(hwnd, -16, style)
    if not w.u.SetParent(hwnd, parent):
        return False
    return w.u.GetParent(hwnd) == parent


def fit_window(hwnd, width, height):
    """
    탭 칸을 꽉 채운다. 제목줄과 테두리는 칸 바깥으로 밀어 잘리게 한다.
    오른쪽 스크롤바는 남긴다(테두리만큼만 넓힌다).
    """
    w = w32()
    if w is None or not hwnd or not w.u.IsWindow(hwnd):
        return
    wr = w.RECT()
    w.u.GetWindowRect(hwnd, w.ctypes.byref(wr))
    p = w.POINT(0, 0)
    w.u.ClientToScreen(hwnd, w.ctypes.byref(p))
    left, top = max(0, p.x - wr.l), max(0, p.y - wr.t)
    # SWP_NOZORDER | SWP_NOACTIVATE
    w.u.SetWindowPos(hwnd, None, -left, -top, max(1, width + 2 * left),
                     max(1, height + top + left), 0x0004 | 0x0010)
    redraw_window(hwnd)


def redraw_window(hwnd):
    """
    다시 그리게 한다. 숨겨 띄운 PuTTY 는 탭에 넣고 보여도, 새 글자가 오기 전에는
    (SSH 키 교환 중 등) 스스로 다시 그리지 않아서 전에 그 자리에 있던 그림이 남는다.
    """
    w = w32()
    if w is not None and hwnd and w.u.IsWindow(hwnd):
        # RDW_INVALIDATE | RDW_ERASE | RDW_FRAME | RDW_ALLCHILDREN | RDW_UPDATENOW
        w.u.RedrawWindow(hwnd, None, None, 0x0001 | 0x0004 | 0x0400 | 0x0080 | 0x0100)


def release_window(hwnd):
    """탭 칸을 지우기 전에 PuTTY 창을 떼어 숨긴다. 칸과 함께 지워지게 두지 않는다."""
    w = w32()
    if w is None or not hwnd or not w.u.IsWindow(hwnd):
        return
    w.u.ShowWindow(hwnd, 0)                                       # SW_HIDE
    w.u.SetParent(hwnd, None)


def show_window(hwnd, how):
    w = w32()
    if w is not None and hwnd:
        w.u.ShowWindow(hwnd, how)


def bring_front(hwnd):
    w = w32()
    if w is not None and hwnd and w.u.IsWindow(hwnd):
        w.u.ShowWindow(hwnd, 9)                                   # SW_RESTORE
        w.u.SetForegroundWindow(hwnd)


def focused_window():
    w = w32()
    return w.u.GetFocus() if w is not None else None


def set_focus(hwnd):
    w = w32()
    if w is not None and hwnd and w.u.IsWindow(hwnd):
        w.u.SetFocus(hwnd)


def toplevel_of(hwnd):
    w = w32()
    return w.u.GetAncestor(hwnd, 2) if w is not None else None    # GA_ROOT


def _clip_open():
    w = w32()
    for _ in range(25):
        if w.u.OpenClipboard(None):
            return True
        time.sleep(0.02)
    return False


def clip_get():
    """클립보드의 글자. 글자가 없으면 None."""
    w = w32()
    if w is None or not w.u.IsClipboardFormatAvailable(13) or not _clip_open():
        return None
    try:
        h = w.u.GetClipboardData(13)                              # CF_UNICODETEXT
        if not h:
            return None
        p = w.k.GlobalLock(h)
        if not p:
            return None
        try:
            return w.ctypes.wstring_at(p)
        finally:
            w.k.GlobalUnlock(h)
    finally:
        w.u.CloseClipboard()


def clip_set(text):
    """클립보드를 text 로. None 이면 비운다."""
    w = w32()
    if w is None or not _clip_open():
        return False
    try:
        w.u.EmptyClipboard()
        if text is None:
            return True
        data = w.ctypes.create_unicode_buffer(text)
        h = w.k.GlobalAlloc(0x0002, w.ctypes.sizeof(data))       # GMEM_MOVEABLE
        p = w.k.GlobalLock(h)
        w.ctypes.memmove(p, data, w.ctypes.sizeof(data))
        w.k.GlobalUnlock(h)
        return bool(w.u.SetClipboardData(13, h))
    finally:
        w.u.CloseClipboard()


def copy_all_id(hwnd):
    """시스템 메뉴에서 「Copy All to Clipboard」 를 글자로 찾는다. 못 찾으면 0x170."""
    w = w32()
    menu = w.u.GetSystemMenu(hwnd, False)
    if menu:
        for i in range(max(0, w.u.GetMenuItemCount(menu))):
            buf = w.ctypes.create_unicode_buffer(128)
            w.u.GetMenuStringW(menu, i, buf, 128, 0x400)          # MF_BYPOSITION
            if "copy all" in buf.value.replace("&", "").lower():
                return w.u.GetMenuItemID(menu, i)
    return IDM_COPYALL


def copy_all(hwnd, wait=None):
    """
    PuTTY 의 화면(스크롤백 포함) 글자. (글자 또는 None, 알림)

    PuTTY 가 클립보드에 쓰므로, 앞뒤로 사용자의 클립보드를 챙겨 되돌린다.
    글자가 아닌 것(그림 등)이 있었으면 되돌리지 못한다 — 알림으로 알린다.
    wait 는 PuTTY 가 다 쓸 때까지 화면을 살려 두는 함수다(tkinter update).
    """
    w = w32()
    if w is None or not hwnd or not w.u.IsWindow(hwnd):
        return None, ""
    had_any = w.u.CountClipboardFormats() > 0
    saved = clip_get()
    seq = w.u.GetClipboardSequenceNumber()
    res = w.ctypes.c_size_t()
    # WM_SYSCOMMAND. SMTO_ABORTIFHUNG: PuTTY 가 멈춰 있으면 기다리지 않는다
    w.u.SendMessageTimeoutW(hwnd, 0x0112, copy_all_id(hwnd), 0, 0x0002, 3000,
                            w.ctypes.byref(res))
    for _ in range(40):
        if w.u.GetClipboardSequenceNumber() != seq:
            break
        if wait:
            wait()
        time.sleep(0.05)
    if w.u.GetClipboardSequenceNumber() == seq:
        return None, ""
    text = clip_get()
    clip_set(saved)
    note = ""
    if had_any and saved is None:
        note = "클립보드에 있던 글자가 아닌 것(그림 등)은 되돌리지 못했습니다."
    return text, note


class FocusBridge(object):
    """
    탭 안의 PuTTY 와 이 창 사이에서 키보드 포커스를 넘긴다.

    PuTTY 를 누르면 → PuTTY 가 마우스를 잡는 순간 PuTTY 에 포커스.
    이 창의 입력칸을 누르면 → 창으로 포커스를 되돌린 뒤 tkinter 가 나눠 준다.
    """

    def __init__(self, root):
        self.root = root
        self.hosts = {}                   # PuTTY hwnd -> 탭 칸(tk Frame)
        self.hooks = {}                   # PuTTY hwnd -> 훅 핸들
        self._cb = None
        w = w32()
        if w is None:
            return
        self._cb = w.WinEventProc(self._on_capture)
        root.bind_all("<Button-1>", self._take_back, add="+")

    def add(self, hwnd, host, pid):
        """
        그 PuTTY 프로세스 하나만 본다. 시스템 전체를 보는 훅은 쓰지 않는다
        (다른 프로그램의 마우스까지 이 프로그램을 지나게 되고, 백신이 키로거로 볼 수 있다).
        """
        self.hosts[hwnd] = host
        w = w32()
        if w is None or self._cb is None or hwnd in self.hooks:
            return
        # EVENT_SYSTEM_CAPTURESTART, WINEVENT_OUTOFCONTEXT. 이 스레드의 메시지
        # 루프(tkinter mainloop)에서 불린다.
        h = w.u.SetWinEventHook(0x0008, 0x0008, None, self._cb, int(pid or 0) or 1, 0, 0)
        if h:
            self.hooks[hwnd] = h
        else:
            log("포커스 훅을 걸지 못함 (PuTTY pid %s)" % pid)

    def remove(self, hwnd):
        self.hosts.pop(hwnd, None)
        h = self.hooks.pop(hwnd, None)
        w = w32()
        if h and w is not None:
            w.u.UnhookWinEvent(h)

    def give(self, hwnd):
        host = self.hosts.get(hwnd)
        if host is None:
            return
        try:
            # tkinter 가 "포커스가 나갔다" 를 알아야 나중에 입력칸을 누를 때
            # 포커스를 다시 가져간다
            host.focus_set()
            self.root.update_idletasks()
        except Exception:                  # noqa: BLE001 - 닫히는 중인 탭
            return
        set_focus(hwnd)

    def _on_capture(self, _hook, _ev, hwnd, _obj, _child, _tid, _t):
        # 콜백 안에서 난 예외는 ctypes 가 삼킨다. 그래도 기록은 남긴다.
        try:
            if hwnd in self.hosts:
                self.give(hwnd)
        except Exception as exc:          # noqa: BLE001
            log("포커스 콜백 예외: %r" % (exc,))

    def _take_back(self, _e):
        if focused_window() in self.hosts:
            set_focus(toplevel_of(self.root.winfo_id()))

    def close(self):
        for hwnd in list(self.hooks):
            self.remove(hwnd)


# ---------------------------------------------------------------------------
# 터널 하나
# ---------------------------------------------------------------------------
class TunnelSession(object):
    """
    터널 하나와 그 위의 PuTTY 하나. 탭 하나가 이것을 하나 쥔다.

    events 큐로 화면에 소식을 보낸다. 화면(tkinter)은 자기 스레드에서만
    만질 수 있으므로 여기서 직접 그리지 않는다. PuTTY 를 탭에 넣는 것도
    화면 스레드가 한다("launched" 를 받고).
    """

    def __init__(self, app, server, info):
        self.app = app
        self.server = server
        self.id = info["tunnel_id"]
        self.stop = threading.Event()
        self.opened = threading.Event()
        self.state = "여는 중"
        self.reason = ""
        self.opened_at = None              # PuTTY 가 붙은 때 (time.time)
        self.listener = None
        self.conn = None
        self.proc = None
        self.port = None
        self.pending = []                 # PuTTY 가 붙기 전에 온 바이트 (sshd 배너)
        self.pending_lock = threading.Lock()
        cfg = app.cfg
        hdr = app.api.headers()
        self.link = Link(cfg["url"], hdr, insecure=bool(cfg.get("insecure")))
        self.down_link = Link(cfg["url"], hdr, insecure=bool(cfg.get("insecure")))

    def _path(self, tail):
        return "/api/client/tunnel/%s/%s" % (self.id, tail)

    def emit(self, text, level="info"):
        self.app.events.put(("tunnel", self, text, level))

    def start(self):
        threading.Thread(target=self._down, daemon=True,
                         name="down-%s" % self.id[:8]).start()

    # --- 챗봇 서버 → PuTTY -----------------------------------------------
    def _down(self):
        try:
            status, frames = self.down_link.stream(self._path("down"))
        except (OSError, http.client.HTTPException) as exc:
            self.finish("챗봇 서버와 연결이 끊어졌습니다 (%s)" % exc)
            return
        if status != 200:
            self.finish((frames or {}).get("error") or "터널을 받지 못했습니다 (%s)" % status)
            return
        try:
            for kind, value in frames:
                if self.stop.is_set():
                    return
                if kind == "O":
                    self.opened.set()
                    self.state = "PuTTY 를 기다리는 중"
                    self.emit("%s · VDI 중계가 붙었습니다. PuTTY 를 띄웁니다"
                              % self.server["name"])
                    threading.Thread(target=self._launch, daemon=True).start()
                elif kind == "D":
                    self._to_putty(value)
                elif kind == "C":
                    self.finish(value or "터널이 닫혔습니다")
                    return
            self.finish("챗봇 서버와 연결이 끊어졌습니다")
        except (OSError, http.client.HTTPException, ValueError) as exc:
            self.finish("챗봇 서버와 연결이 끊어졌습니다 (%s)" % exc)

    def _to_putty(self, data):
        with self.pending_lock:
            if self.conn is None:
                self.pending.append(data)
                return
            conn = self.conn
        try:
            conn.sendall(data)
        except OSError:
            self.finish("PuTTY 와의 연결이 끊어졌습니다")

    # --- PuTTY 띄우기 ----------------------------------------------------
    def _listen(self):
        for port in (local_port_for(self.server["id"]), 0):
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                # 127.0.0.1 만. 0.0.0.0 에 열면 같은 망의 다른 PC 가 붙는다.
                s.bind(("127.0.0.1", port))
                s.listen(1)
                s.settimeout(ACCEPT_SECONDS)
                return s
            except OSError:
                s.close()
        raise OSError("로컬 포트를 열지 못했습니다")

    def putty_argv(self, exe, port):
        argv = [exe, "-ssh", "-P", str(port), "-l", self.server["username"]]
        if self.app.cfg.get("use_loghost", True):
            # 제목과 호스트 키를 127.0.0.1 이 아니라 서버 이름으로 기억하게 한다.
            argv += ["-loghost", self.server["name"]]
        argv.append("127.0.0.1")
        return argv

    def _launch(self):
        exe = putty_path(self.app.cfg)
        if not exe:
            self.finish("putty.exe 를 찾지 못했습니다. 이 프로그램과 같은 폴더에 두세요. "
                        "하위 폴더는 보지 않습니다.", close_remote=True)
            return
        try:
            self.listener = self._listen()
        except OSError as exc:
            self.finish(str(exc), close_remote=True)
            return
        self.port = self.listener.getsockname()[1]
        kw = {"close_fds": True}
        if os.name == "nt" and getattr(self.app, "embed", False):
            # 숨긴 채로 띄운다. 화면 스레드가 탭에 넣은 뒤에 보인다(깜박임 없음).
            si = subprocess.STARTUPINFO()
            si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            si.wShowWindow = 0                                    # SW_HIDE
            kw["startupinfo"] = si
        try:
            self.proc = subprocess.Popen(self.putty_argv(exe, self.port), **kw)
        except OSError as exc:
            self.finish("PuTTY 를 띄우지 못했습니다 (%s)" % exc, close_remote=True)
            return
        log("터널 %s PuTTY 띄움 pid=%s port=%s" % (
            self.id[:8], getattr(self.proc, "pid", None), self.port))
        self.app.events.put(("launched", self, None, None))
        try:
            conn, peer = self.listener.accept()
        except OSError:
            self.finish("PuTTY 가 %d초 안에 붙지 않아 닫았습니다" % ACCEPT_SECONDS,
                        close_remote=True)
            return
        finally:
            # 한 연결만 받는다. 받은 뒤에는 리스너를 닫는다.
            try:
                self.listener.close()
            except OSError:
                pass
        pid = owner_pid(self.port, peer[1])
        if pid is not None and self.proc is not None and pid != self.proc.pid:
            try:
                conn.close()
            except OSError:
                pass
            self.finish("띄운 PuTTY 가 아닌 다른 프로그램(PID %d)이 먼저 붙어 끊었습니다"
                        % pid, close_remote=True)
            return
        conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        conn.settimeout(None)
        with self.pending_lock:
            self.conn = conn
            backlog, self.pending = self.pending, []
        try:
            for chunk in backlog:
                conn.sendall(chunk)
        except OSError:
            self.finish("PuTTY 와의 연결이 끊어졌습니다", close_remote=True)
            return
        self.state = "열림"
        self.opened_at = time.time()
        self.emit("%s · 열렸습니다. 탭 안의 PuTTY 에서 비밀번호를 치세요" % self.server["name"])
        self._up(conn)

    # --- PuTTY → 챗봇 서버 -----------------------------------------------
    def _up(self, conn):
        seq = [0]
        while not self.stop.is_set():
            try:
                data = conn.recv(32768)
            except OSError:
                data = b""
            if not data:
                self.finish("PuTTY 세션이 끝났습니다 (exit 또는 PuTTY 를 닫음)",
                            close_remote=True)
                return
            if not send_chunks(self.link, self._path("up"), seq, data, self.stop):
                if not self.stop.is_set():
                    # 이유는 아래 스트림의 C 프레임이 곧 알려 준다
                    time.sleep(1.0)
                    self.finish(self.reason or "터널이 닫혔습니다")
                return

    # --- 닫기 -----------------------------------------------------------
    def close(self, why="사용자가 닫았습니다"):
        self.finish(why, close_remote=True)

    def kill_putty(self):
        """PuTTY 를 먼저 끈다. 소켓을 먼저 닫으면 PuTTY 가 「Network error」 창을 띄운다."""
        proc = self.proc
        if proc is None or not hasattr(proc, "terminate"):
            return
        try:
            if proc.poll() is None:
                proc.terminate()
        except OSError:
            pass

    def finish(self, reason, close_remote=False):
        if self.stop.is_set():
            return
        self.stop.set()
        log("터널 %s 닫힘 (%s): %s" % (self.id[:8], self.server.get("name"), reason))
        self.state = "닫힘"
        self.reason = reason
        self.kill_putty()
        if close_remote:
            try:
                self.link.post(self._path("close"), {"reason": reason})
            except (OSError, http.client.HTTPException):
                pass
        for s in (self.conn, self.listener):
            if s is None:
                continue
            try:
                s.close()
            except OSError:
                pass
        self.link.close()
        self.down_link.close()
        self.app.events.put(("closed", self, reason, "warn"))


# ---------------------------------------------------------------------------
# 화면
# ---------------------------------------------------------------------------
INK = "#16231F"
MUTED = "#5A6B65"
FAINT = "#9FADA8"
LINE = "#CBD4D0"
ACCENT = "#1C6A58"
ACCENT_DARK = "#124437"
ACCENT_SOFT = "#E3EFEB"
ACCENT_LINE = "#BFDCD3"
WARN = "#A8491A"
WARN_SOFT = "#F8EBE2"
WARN_LINE = "#E0C3AE"
AMBER = "#E0B44A"
GROUND = "#FFFFFF"
PAPER = "#FDFEFD"
PANEL = "#F4F6F5"
SIDE = "#FBFCFB"
TAB_OFF = "#E6EBE9"
TERM = "#0E1C18"
TERM_BAR = "#13261F"
TERM_LINE = "#23392F"
TERM_TEXT = "#9FC7B8"
TERM_SOFT = "#6F9E8C"
TERM_INK = "#E8F2EE"
MINT = "#86DCB8"

F = ("Malgun Gothic", 10)
F_B = ("Malgun Gothic", 10, "bold")
F_S = ("Malgun Gothic", 9)
F_S_B = ("Malgun Gothic", 9, "bold")
F_T = ("Malgun Gothic", 11, "bold")
F_H = ("Malgun Gothic", 15, "bold")
MONO = ("Consolas", 10)
MONO_S = ("Consolas", 9)

CMD_STATE = {"pending": "승인 대기", "running": "실행 중", "done": "실행됨",
             "failed": "실패", "rejected": "거절됨", "expired": "시간이 지나 취소",
             "blocked": "막힘", "denied": "거부됨"}


def run_gui():
    import tkinter as tk
    from tkinter import filedialog, ttk

    dpi_aware()
    root = tk.Tk()

    def report(exc_type, exc, tb):
        import traceback
        text = "".join(traceback.format_exception(exc_type, exc, tb)).rstrip()
        log("화면 콜백 예외: %s" % text)
        try:
            app.events.put(("say", "오류가 났습니다. %s 를 보내 주세요. (%s)"
                            % (LOG_PATH, exc), "warn", None))
        except NameError:
            pass

    root.report_callback_exception = report
    root.title("%s %s" % (APP_NAME, VERSION))
    scale = max(1.0, root.winfo_fpixels("1i") / 96.0)
    # 화면보다 크게 열면 아래 입력칸이 작업 표시줄에 가린다
    width = min(int(1320 * scale), root.winfo_screenwidth() - 40)
    height = min(int(860 * scale), root.winfo_screenheight() - 90)
    root.geometry("%dx%d+%d+%d" % (width, height, 20, 10))
    root.minsize(min(int(1000 * scale), width), min(int(600 * scale), height))
    root.configure(bg=GROUND)
    root.option_add("*Font", F)

    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except tk.TclError:
        pass
    style.configure("TCombobox", padding=4)

    BUTTONS = {
        "accent": (ACCENT, "#FFFFFF", ACCENT_DARK, ACCENT),
        "plain": (GROUND, INK, PANEL, LINE),
        "soft": (PANEL, INK, TAB_OFF, LINE),
        "warn": (WARN, "#FFFFFF", "#8A3B14", WARN),
        "mint": (MINT, TERM, "#A6E8CB", MINT),
        "ghost": (TERM_BAR, "#CFE3DB", "#1B3329", "#3B5A4E"),
    }

    def button(parent, text, cmd, kind="plain", font=F, **kw):
        bg, fg, active, edge = BUTTONS[kind]
        return tk.Button(parent, text=text, command=cmd, bg=bg, fg=fg, font=font,
                         activebackground=active, activeforeground=fg, relief="flat",
                         bd=0, padx=kw.pop("padx", 12), pady=kw.pop("pady", 4),
                         cursor="hand2", highlightthickness=1, highlightbackground=edge,
                         highlightcolor=edge, disabledforeground=FAINT, **kw)

    def dot(parent, color, bg, size=8):
        c = tk.Canvas(parent, width=size, height=size, bg=bg, highlightthickness=0, bd=0)
        c.create_oval(0, 0, size - 1, size - 1, fill=color, outline=color)
        return c

    def bind_tree(widget, seq, fn):
        widget.bind(seq, fn)
        for child in widget.winfo_children():
            bind_tree(child, seq, fn)

    state = {"cfg": load_config(), "frame": None}
    app = types.SimpleNamespace(events=queue.Queue(), generation=0, cfg=None, api=None,
                                embed=w32() is not None)
    focus = FocusBridge(root)

    def clear_root():
        if state["frame"] is not None:
            state["frame"].destroy()
            state["frame"] = None

    # --- C-1 등록 --------------------------------------------------------
    def show_register(message=""):
        app.generation += 1
        clear_root()
        f = tk.Frame(root, bg=GROUND, padx=40, pady=36)
        f.pack(fill="both", expand=True)
        state["frame"] = f
        tk.Label(f, text="처음 한 번만 등록합니다", font=F_H, bg=GROUND, fg=INK).pack(
            anchor="w")
        tk.Label(f, bg=GROUND, fg=MUTED, wraplength=760, justify="left",
                 text="웹의 서버 화면에서 「내 클라이언트」 코드를 받아 넣으면 이 PC 가 "
                      "내 클라이언트가 됩니다. 남의 코드로는 등록되지 않습니다.").pack(
            anchor="w", pady=(4, 18))

        row = tk.Frame(f, bg=GROUND)
        row.pack(fill="x")
        tk.Label(row, text="챗봇 서버 주소", bg=GROUND, fg=INK).grid(row=0, column=0,
                                                                 sticky="w")
        tk.Label(row, text="등록 코드", bg=GROUND, fg=INK).grid(row=0, column=1, sticky="w",
                                                            padx=(14, 0))
        url = tk.StringVar(value=(state["cfg"] or {}).get("url", "https://"))
        code = tk.StringVar()
        insecure = tk.BooleanVar(value=bool((state["cfg"] or {}).get("insecure")))
        e_url = tk.Entry(row, textvariable=url, width=52, font=MONO, relief="flat",
                         highlightthickness=1, highlightbackground=LINE,
                         highlightcolor=ACCENT)
        e_url.grid(row=1, column=0, sticky="we", pady=4, ipady=4)
        e_code = tk.Entry(row, textvariable=code, width=12, font=("Consolas", 13),
                          relief="flat", highlightthickness=1, highlightbackground=LINE,
                          highlightcolor=ACCENT)
        e_code.grid(row=1, column=1, sticky="w", padx=(14, 0), pady=4, ipady=2)
        btn = button(row, "등록", None, "accent", font=F_B)
        btn.grid(row=1, column=2, padx=(14, 0))
        tk.Checkbutton(f, text="사내 사설 인증서 (인증서 검사를 하지 않음)",
                       variable=insecure, bg=GROUND, fg=INK, activebackground=GROUND,
                       selectcolor=GROUND).pack(anchor="w", pady=(6, 0))

        msg = tk.Label(f, text=message, bg=GROUND, fg=WARN, wraplength=760, justify="left")
        msg.pack(anchor="w", pady=(14, 0))

        notes = ("1   코드는 10분 뒤에 만료됩니다. 새로 받으면 전에 받은 코드만 죽습니다.\n"
                 "2   등록하면 키가 %s 에 저장됩니다. 대상 서버 비밀번호는 저장하지 않습니다.\n"
                 "3   putty.exe 는 이 프로그램과 같은 폴더에 있어야 합니다. 지금: %s"
                 % (CONFIG_PATH, putty_path(state["cfg"]) or "없음"))
        tk.Label(f, text=notes, bg=GROUND, fg=MUTED, justify="left").pack(
            anchor="w", pady=(18, 0))

        def do_register():
            u = url.get().strip()
            if not u.startswith(("http://", "https://")):
                u = "https://" + u
            api = Api(u, insecure=insecure.get())
            btn.configure(state="disabled")
            msg.configure(text="등록하는 중...")

            def work():
                try:
                    res = api.call("POST", "/api/client/register", {
                        "code": code.get().strip(),
                        "name": os.environ.get("COMPUTERNAME") or socket.gethostname(),
                        "version": VERSION, "os": "%s %s" % (os.name, sys.platform)})
                except ApiError as exc:
                    text = exc.message      # exc 는 except 블록이 끝나면 사라진다
                    root.after(0, lambda: (btn.configure(state="normal"),
                                           msg.configure(text=text)))
                    return
                cfg = dict(state["cfg"] or {})
                cfg.update({"url": u, "client_key": res["client_key"],
                            "insecure": insecure.get(),
                            "registered_at": time.strftime("%Y-%m-%d %H:%M:%S")})
                save_config(cfg)
                state["cfg"] = cfg
                root.after(0, show_main)

            threading.Thread(target=work, daemon=True).start()

        btn.configure(command=do_register)
        e_code.bind("<Return>", lambda _e: do_register())
        (e_code if url.get() not in ("", "https://") else e_url).focus_set()

    # --- C-2 메인 창 ------------------------------------------------------
    def show_main():
        app.generation += 1
        gen = app.generation
        clear_root()
        cfg = state["cfg"]
        app.cfg = cfg
        app.api = Api(cfg["url"], cfg.get("client_key"), insecure=bool(cfg.get("insecure")))
        app.me = None
        app.servers = []
        app.can_tunnel = False
        app.has_chat = False
        app.tab_max = 4
        app.tabs = []
        app.active = None
        app.target = None                  # Claude 패널의 대상 서버 id
        app.sessions = {}                  # 서버 id -> 대화
        app.session_lock = threading.Lock()
        app.messages = {}                  # 서버 id -> 마지막으로 받은 메시지
        app.busy = set()                   # 답을 기다리는 서버 id
        app.shot = None                    # 입력칸 위의 화면 칩

        outer = tk.Frame(root, bg=GROUND)
        outer.pack(fill="both", expand=True)
        state["frame"] = outer

        # ── 위: 제목과 상태 ──
        top = tk.Frame(outer, bg=PANEL, height=46, highlightthickness=0)
        top.pack(fill="x", side="top")
        top.pack_propagate(False)
        tk.Frame(outer, bg=LINE, height=1).pack(fill="x", side="top")
        tk.Label(top, text=APP_NAME, font=F_B, bg=PANEL, fg=INK).pack(side="left",
                                                                      padx=(16, 6))
        tk.Label(top, text=VERSION, font=MONO_S, bg=PANEL, fg=MUTED).pack(side="left")
        tk.Frame(top, bg=LINE, width=1, height=18).pack(side="left", padx=12)
        relay_pill = tk.Label(top, text="중계 확인 중", font=F_S, bg=GROUND, fg=MUTED,
                              padx=10, pady=3, highlightthickness=1,
                              highlightbackground=LINE)
        relay_pill.pack(side="left")
        tab_pill = tk.Label(top, text="탭 0 / -", font=F_S, bg=GROUND, fg=MUTED, padx=10,
                            pady=3, highlightthickness=1, highlightbackground=LINE)
        tab_pill.pack(side="left", padx=8)
        button(top, "설정", lambda: show_settings()).pack(side="right", padx=16)

        # ── 아래: 상태줄 ──
        foot = tk.Frame(outer, bg=PANEL, height=30)
        foot.pack(fill="x", side="bottom")
        foot.pack_propagate(False)
        tk.Frame(outer, bg=LINE, height=1).pack(fill="x", side="bottom")
        foot_tabs = tk.Label(foot, text="", font=MONO_S, bg=PANEL, fg=MUTED)
        foot_tabs.pack(side="left", padx=(16, 18))
        foot_tab = tk.Label(foot, text="", font=MONO_S, bg=PANEL, fg=MUTED)
        foot_tab.pack(side="left", padx=(0, 18))
        foot_msg = tk.Label(foot, text="", font=F_S, bg=PANEL, fg=MUTED, anchor="w")
        foot_msg.pack(side="left", fill="x", expand=True)

        body = tk.Frame(outer, bg=GROUND)
        body.pack(fill="both", expand=True)

        # ── 왼쪽: 서버 ──
        left = tk.Frame(body, bg=SIDE, width=int(210 * scale))
        left.pack(side="left", fill="y")
        left.pack_propagate(False)
        tk.Frame(body, bg=LINE, width=1).pack(side="left", fill="y")
        tk.Label(left, text="서버", font=F_S_B, bg=SIDE, fg=MUTED).pack(
            anchor="w", padx=18, pady=(14, 6))
        server_box = tk.Frame(left, bg=SIDE)
        server_box.pack(fill="x", padx=12)
        tk.Label(left, text="웹에서 관리자가 등록한 것만 보입니다. 누르면 그 서버의 탭이 "
                            "열립니다.", font=F_S, bg=SIDE, fg=MUTED, justify="left",
                 wraplength=int(180 * scale)).pack(side="bottom", anchor="w", padx=18,
                                                    pady=14)
        why_lbl = tk.Label(left, text="", font=F_S, bg=SIDE, fg=WARN, justify="left",
                           wraplength=int(180 * scale))
        why_lbl.pack(side="bottom", anchor="w", padx=18)

        # ── 오른쪽: Claude (C-3) ──
        right = tk.Frame(body, bg=PAPER, width=int(380 * scale))
        right.pack(side="right", fill="y")
        right.pack_propagate(False)
        tk.Frame(body, bg=LINE, width=1).pack(side="right", fill="y")

        head = tk.Frame(right, bg=PAPER)
        head.pack(fill="x", side="top")
        tk.Label(head, text="Claude", font=("Malgun Gothic", 12, "bold"), bg=PAPER,
                 fg=INK).pack(side="left", padx=(16, 0), pady=10)
        target_box = ttk.Combobox(head, state="readonly", width=20, font=F)
        target_box.pack(side="right", padx=(6, 14))
        tk.Label(head, text="대상", font=F_S, bg=PAPER, fg=MUTED).pack(side="right")
        tk.Frame(right, bg=LINE, height=1).pack(fill="x", side="top")

        # 입력칸을 **먼저** 아래에 붙인다. 1.0.0 은 대화칸을 먼저 쌓아서, 창이 낮으면
        # 입력칸이 밀려나 보이지 않거나 눌리지 않았다.
        composer = tk.Frame(right, bg=PAPER)
        composer.pack(fill="x", side="bottom")
        tk.Frame(right, bg=LINE, height=1).pack(fill="x", side="bottom")
        chip_row = tk.Frame(composer, bg=PAPER)
        chip_lbl = tk.Label(chip_row, text="", font=F_S, bg=ACCENT_SOFT, fg=ACCENT, padx=8,
                            pady=2, highlightthickness=1, highlightbackground=ACCENT_LINE)
        chip_lbl.pack(side="left")
        chip_x = tk.Label(chip_row, text=" × ", font=F_B, bg=ACCENT_SOFT, fg=ACCENT,
                          cursor="hand2", highlightthickness=1,
                          highlightbackground=ACCENT_LINE)
        chip_x.pack(side="left")
        tk.Label(chip_row, text="× 로 뺄 수 있음", font=F_S, bg=PAPER,
                 fg=MUTED).pack(side="left", padx=8)
        ask_wrap = tk.Frame(composer, bg=PAPER)
        ask_wrap.pack(fill="x", padx=16, pady=(12, 8))
        ask = tk.Text(ask_wrap, height=4, wrap="word", font=F, relief="flat", bg=GROUND,
                      fg=INK, padx=9, pady=7, highlightthickness=1,
                      highlightbackground=ACCENT, highlightcolor=ACCENT, undo=True)
        ask.pack(fill="x")
        hint = tk.Label(ask_wrap, text="", font=F, bg=GROUND, fg=FAINT, cursor="xterm")
        btn_row = tk.Frame(composer, bg=PAPER)
        btn_row.pack(fill="x", padx=16, pady=(0, 12))
        b_send = button(btn_row, "보내기", None, "accent", font=F_B, padx=18, pady=6)
        b_send.pack(side="right")
        b_paste = button(btn_row, "PuTTY 에서 복사한 것 붙여넣기", None, "soft", pady=6)
        b_paste.pack(side="left", fill="x", expand=True, padx=(0, 8))

        chat_wrap = tk.Frame(right, bg=PAPER)
        chat_wrap.pack(fill="both", expand=True)
        chat_bar = tk.Scrollbar(chat_wrap, orient="vertical")
        chat_bar.pack(side="right", fill="y")
        chat = tk.Text(chat_wrap, wrap="word", bg=PAPER, fg=INK, relief="flat", font=F,
                      padx=14, pady=10, state="disabled", highlightthickness=0,
                      yscrollcommand=chat_bar.set, cursor="arrow", spacing1=2, spacing3=2)
        chat.pack(side="left", fill="both", expand=True)
        chat_bar.configure(command=chat.yview)
        chat.tag_configure("me_h", foreground=ACCENT, font=F_S_B, justify="right")
        chat.tag_configure("me", foreground=INK, justify="right", lmargin1=60, lmargin2=60)
        chat.tag_configure("bot_h", foreground=MUTED, font=F_S_B)
        chat.tag_configure("bot", foreground=INK, rmargin=20)
        chat.tag_configure("chip", foreground=ACCENT, background=ACCENT_SOFT, font=F_S,
                          justify="right")
        chat.tag_configure("cmd", foreground=MUTED, font=MONO_S, lmargin1=8, lmargin2=8)
        chat.tag_configure("warn", foreground=WARN)
        chat.tag_configure("muted", foreground=MUTED, font=F_S)

        # ── 가운데: 탭 + 탭 안의 PuTTY ──
        center = tk.Frame(body, bg=TERM)
        center.pack(side="left", fill="both", expand=True)
        strip = tk.Frame(center, bg=PANEL, height=int(40 * scale))
        strip.pack(fill="x", side="top")
        strip.pack_propagate(False)
        tk.Frame(center, bg=LINE, height=1).pack(fill="x", side="top")
        pages = tk.Frame(center, bg=TERM)
        pages.pack(fill="both", expand=True)
        empty = tk.Frame(pages, bg=TERM)
        empty_lbl = tk.Label(empty, text="", font=F, bg=TERM, fg=TERM_TEXT, justify="center",
                             wraplength=int(520 * scale))
        empty_lbl.place(relx=0.5, rely=0.45, anchor="center")

        # --- 도우미 -----------------------------------------------------
        def say(text, level="info"):
            foot_msg.configure(text=text, fg=WARN if level == "warn" else MUTED)

        def server_by_id(sid):
            for s in app.servers:
                if s["id"] == sid:
                    return s
            return None

        def tab_for_server(sid):
            for t in app.tabs:
                if t.server["id"] == sid:
                    return t
            return None

        def tab_for_session(sess):
            for t in app.tabs:
                if t.sess is sess:
                    return t
            return None

        def live_tabs():
            return [t for t in app.tabs if t.phase != "closed"]

        def block_reason():
            """누를 수 없으면 이유. 막는 것은 서버다(눌러도 서버가 거절)."""
            if app.me and not app.me.get("version_ok", True):
                return ("이 클라이언트가 낡았습니다. 서버가 %s 이상을 요구합니다. 웹에서 "
                        "새로 받아 주세요." % app.me.get("min_version"))
            if app.me is None:
                return "챗봇 서버에 묻는 중입니다."
            if not app.can_tunnel:
                return "PuTTY 탭을 열 허용이 없습니다. 관리자에게 요청하세요."
            if not app.me["relay"]["connected"]:
                return ("내 VDI 의 중계 프로그램이 붙어 있지 않습니다. 웹의 서버 화면 → "
                        "「내 중계」 에서 설치하고 등록 코드를 넣으세요.")
            if not putty_path(app.cfg):
                return ("putty.exe 를 찾지 못했습니다. 이 프로그램과 같은 폴더(%s)에 "
                        "두세요." % here())
            return ""

        def minutes(since):
            if not since:
                return ""
            m = int((time.time() - since) // 60)
            return "%d분" % m if m < 60 else "%d시간 %d분" % (m // 60, m % 60)

        # --- 탭 하나 (C-2 가운데, C-8 상태) ---------------------------------
        class Tab(object):
            def __init__(self, server):
                self.server = server
                self.sess = None
                self.hwnd = None
                self.embedded = False
                self.phase = "opening"         # opening | open | closed
                self.reason = ""
                self.launched = False
                self.embed_tried = False       # 창을 찾아 넣어 봤다 (실패하면 별도 창)
                self.dialog = None             # PuTTY 가 띄운 대화상자 (호스트 키 등)

                self.page = tk.Frame(pages, bg=TERM)
                bar = tk.Frame(self.page, bg=TERM_BAR, height=int(38 * scale))
                bar.pack(fill="x", side="top")
                bar.pack_propagate(False)
                tk.Frame(self.page, bg=TERM_LINE, height=1).pack(fill="x", side="top")
                self.info = tk.Label(bar, text="", font=MONO_S, bg=TERM_BAR, fg=TERM_TEXT)
                self.info.pack(side="left", padx=12)
                self.b_close = button(bar, "탭 닫기", lambda: close_tab(self), "ghost",
                                      font=F_S, padx=10, pady=2)
                self.b_close.pack(side="right", padx=(6, 12))
                self.b_shot = button(bar, "이 화면을 Claude 에게", lambda: send_screen(self),
                                     "mint", font=F_S_B, padx=11, pady=2)
                self.b_shot.pack(side="right")
                self.b_front = button(bar, "앞으로 가져오기", lambda: bring_front(self.hwnd),
                                      "ghost", font=F_S, padx=10, pady=2)
                self.b_dialog = button(bar, "PuTTY 확인 창 보기",
                                       lambda: bring_dialog(self.dialog, toplevel_of(
                                           root.winfo_id())), "mint", font=F_S_B,
                                       padx=10, pady=2)

                self.stage = tk.Frame(self.page, bg=TERM)
                self.stage.pack(fill="both", expand=True)
                # PuTTY 가 들어갈 칸. 탭을 바꾸면 이 칸이 숨고, 그 안의 PuTTY 도 숨는다.
                self.host = tk.Frame(self.stage, bg=TERM, takefocus=0)
                self.host.bind("<Configure>", lambda _e: self.fit())
                self.note = tk.Frame(self.stage, bg=TERM)

                self.item = tk.Frame(strip, bg=TAB_OFF, cursor="hand2", padx=12)
                self.item_dot = tk.Canvas(self.item, width=8, height=8, highlightthickness=0,
                                          bd=0, bg=TAB_OFF)
                self.item_dot.pack(side="left")
                self.item_name = tk.Label(self.item, text=server["name"], font=F, bg=TAB_OFF,
                                          fg=INK)
                self.item_name.pack(side="left", padx=(7, 6))
                self.item_x = tk.Label(self.item, text="×", font=F, bg=TAB_OFF, fg=FAINT,
                                       cursor="hand2")
                self.item_x.pack(side="left")
                self.item.pack(side="left", padx=(0, 2), pady=(6, 0), fill="y")
                bind_tree(self.item, "<Button-1>", lambda _e: activate(self))
                self.item_x.bind("<Button-1>", lambda _e: (close_tab(self), "break")[1])

            # 그리기
            def paint(self):
                on = app.active is self
                bg = TERM if on else TAB_OFF
                color = {"opening": AMBER, "closed": WARN}.get(
                    self.phase, MINT if on else ACCENT)
                self.item.configure(bg=bg)
                self.item_dot.configure(bg=bg)
                self.item_dot.delete("all")
                self.item_dot.create_oval(0, 0, 7, 7, fill=color, outline=color)
                self.item_name.configure(bg=bg, fg=TERM_INK if on else INK,
                                         font=F_B if on else F)
                self.item_x.configure(bg=bg, fg=TERM_SOFT if on else FAINT)
                who = "%s@%s" % (self.server.get("username") or "?", self.server["name"])
                if self.phase == "open":
                    st = "열림 %s" % minutes(self.sess.opened_at if self.sess else None)
                    if not self.embedded:
                        st += " · 별도 창"
                elif self.phase == "opening":
                    st = "여는 중"
                else:
                    st = "끊김"
                self.info.configure(text="%s · %s" % (who, st))
                ready = self.phase == "open" and bool(self.hwnd)
                self.b_shot.configure(state="normal" if ready else "disabled",
                                      bg=MINT if ready else TERM_LINE,
                                      highlightbackground=MINT if ready else TERM_LINE)
                if (self.phase == "open" or self.embed_tried) and self.hwnd                         and not self.embedded and self.phase != "closed":
                    if not self.b_front.winfo_ismapped():
                        self.b_front.pack(side="right", padx=6)
                else:
                    self.b_front.pack_forget()
                if self.dialog and self.phase != "closed":
                    if not self.b_dialog.winfo_ismapped():
                        self.b_dialog.pack(side="right", padx=6)
                else:
                    self.b_dialog.pack_forget()

            def show_body(self):
                for w in self.note.winfo_children():
                    w.destroy()
                if self.embedded and self.phase != "closed":
                    self.note.pack_forget()
                    self.host.pack(fill="both", expand=True)
                    return
                self.host.pack_forget()
                self.note.pack(fill="both", expand=True)
                if self.phase == "closed":
                    self.note.configure(bg=PAPER)
                    box = tk.Frame(self.note, bg=PAPER)
                    box.place(relx=0.5, rely=0.42, anchor="center")
                    tk.Label(box, text=self.reason or "닫혔습니다", font=F_T, bg=PAPER,
                             fg=WARN, wraplength=int(520 * scale), justify="left").pack(
                        anchor="w")
                    tk.Label(box, text="PuTTY 는 끊긴 이유를 「Network error」 로만 압니다. "
                                       "이유는 여기 적습니다. 다시 열면 비밀번호를 다시 "
                                       "칩니다.", font=F_S, bg=PAPER, fg=MUTED,
                             wraplength=int(520 * scale), justify="left").pack(
                        anchor="w", pady=(6, 12))
                    row = tk.Frame(box, bg=PAPER)
                    row.pack(anchor="w")
                    button(row, "다시 열기", lambda: reopen(self), "accent", font=F_B).pack(
                        side="left")
                    button(row, "탭 닫기", lambda: close_tab(self)).pack(side="left", padx=8)
                    return
                self.note.configure(bg=TERM)
                box = tk.Frame(self.note, bg=TERM)
                box.place(relx=0.5, rely=0.45, anchor="center")
                if (self.phase == "open" or self.embed_tried) and not self.embedded:
                    big = "PuTTY 를 탭 안에 넣지 못해 별도 창으로 열었습니다."
                    small = ("터널은 같은 것이라 접속에는 영향이 없습니다. 위의 「앞으로 "
                             "가져오기」 로 그 창을 앞으로 부릅니다.")
                elif self.launched:
                    big = "PuTTY 를 탭에 넣는 중…"
                    small = "호스트 키를 묻는 창이 뜨면 서버 지문을 확인하고 누르세요."
                else:
                    big = "VDI 중계가 %s 에 붙는 중…" % (self.server.get("address")
                                                    or self.server["name"])
                    small = "30초 안에 붙지 않으면 이유를 적고 멈춥니다."
                tk.Label(box, text=big, font=F, bg=TERM, fg=TERM_TEXT).pack()
                tk.Label(box, text=small, font=F_S, bg=TERM, fg=TERM_SOFT,
                         wraplength=int(520 * scale)).pack(pady=(6, 0))

            def fit(self):
                if self.embedded and self.hwnd:
                    fit_window(self.hwnd, self.host.winfo_width(), self.host.winfo_height())

            def forget_window(self):
                if self.hwnd:
                    focus.remove(self.hwnd)
                    release_window(self.hwnd)
                self.hwnd = None
                self.embedded = False
                self.embed_tried = False

        # --- 탭 다루기 ---------------------------------------------------
        def render_center():
            if app.active is None:
                for t in app.tabs:
                    t.page.pack_forget()
                why = block_reason()
                empty_lbl.configure(
                    text="왼쪽에서 서버를 누르면 여기에 그 서버의 PuTTY 가 탭으로 열립니다."
                         + ("\n\n" + why if why else ""))
                empty.pack(fill="both", expand=True)
            else:
                empty.pack_forget()
            for t in app.tabs:
                t.paint()
            render_counts()

        def activate(tab):
            if app.active is not None and app.active is not tab:
                app.active.page.pack_forget()
            app.active = tab
            empty.pack_forget()
            tab.page.pack(fill="both", expand=True)
            tab.show_body()
            render_center()
            render_servers()
            set_target(tab.server["id"])
            if tab.embedded and tab.hwnd:
                root.update_idletasks()
                tab.fit()
                focus.give(tab.hwnd)

        def open_tab(server):
            existing = tab_for_server(server["id"])
            if existing is not None:
                activate(existing)
                return
            why = block_reason()
            if why:
                say(why, "warn")
                return
            if len(live_tabs()) >= app.tab_max:
                say("PuTTY 탭은 한 사람에 %d개까지입니다. 쓰지 않는 탭을 먼저 닫아 "
                    "주세요. (관리자가 웹의 중계 설정에서 바꿀 수 있습니다)" % app.tab_max,
                    "warn")
                return
            tab = Tab(dict(server))
            app.tabs.append(tab)
            activate(tab)
            start_tunnel(tab)

        def start_tunnel(tab):
            tab.phase = "opening"
            tab.reason = ""
            tab.launched = False
            tab.forget_window()
            tab.show_body()
            render_center()
            render_servers()
            say("%s · 터널을 여는 중..." % tab.server["name"])
            server = tab.server

            def work():
                try:
                    info = app.api.call("POST", "/api/client/tunnel",
                                        {"server_id": server["id"]})
                except ApiError as exc:
                    app.events.put(("open_failed", tab, exc.message, None))
                    return
                srv = dict(server)
                srv.update(info.get("server") or {})
                sess = TunnelSession(app, srv, info)
                app.events.put(("started", tab, sess, None))
                sess.start()

            threading.Thread(target=work, daemon=True).start()

        def reopen(tab):
            if tab.phase != "closed":
                return
            why = block_reason()
            if why:
                say(why, "warn")
                return
            if len(live_tabs()) >= app.tab_max:
                say("PuTTY 탭은 한 사람에 %d개까지입니다. 다른 탭을 먼저 닫아 주세요."
                    % app.tab_max, "warn")
                return
            start_tunnel(tab)

        def close_tab(tab):
            sess = tab.sess
            if sess is not None:
                # 칸을 지우기 전에 PuTTY 를 끈다. 칸이 먼저 사라지면 그 안의 PuTTY 가
                # 주인 없는 창으로 남는다.
                sess.kill_putty()
                threading.Thread(target=sess.close, args=("탭을 닫았습니다",),
                                 daemon=True).start()
            tab.forget_window()
            idx = app.tabs.index(tab)
            app.tabs.remove(tab)
            tab.item.destroy()
            tab.page.destroy()
            if app.active is tab:
                app.active = None
                if app.tabs:
                    activate(app.tabs[min(idx, len(app.tabs) - 1)])
                    return
            render_center()
            render_servers()
            render_targets()

        def on_found(sess, hwnd):
            tab = tab_for_session(sess)
            if tab is None or sess.stop.is_set():
                return
            tab.hwnd = hwnd
            log("탭 %s PuTTY 창 %s" % (tab.server["name"], hwnd or "못 찾음"))
            if hwnd and app.embed and embed_window(hwnd, tab.host.winfo_id()):
                tab.embedded = True
                focus.add(hwnd, tab.host, getattr(sess.proc, "pid", 0))
                tab.show_body()
                show_window(hwnd, 5)                              # SW_SHOW
                root.update_idletasks()
                tab.fit()
                if app.active is tab:
                    focus.give(hwnd)
            else:
                # C-8 「탭이 안 될 때」: 별도 창으로 둔다. 터널은 같다.
                w = w32()
                log("탭 %s 안에 넣지 못함 (창 %s, 오류 %s)" % (
                    tab.server["name"], hwnd,
                    w.ctypes.get_last_error() if w is not None else "-"))
                tab.embedded = False
                tab.embed_tried = True
                if hwnd:
                    show_window(hwnd, 5)
                tab.show_body()
                say("%s · PuTTY 를 탭에 넣지 못해 별도 창으로 열었습니다"
                    % tab.server["name"], "warn")
            tab.paint()

        def find_window(sess):
            pid = getattr(sess.proc, "pid", None)
            end = time.time() + EMBED_SECONDS
            hwnd = None
            while pid and time.time() < end and not sess.stop.is_set():
                hwnd = find_putty_window(pid)
                if hwnd:
                    break
                time.sleep(0.1)
            app.events.put(("found", sess, hwnd, None))

        # --- 왼쪽 서버 목록 ------------------------------------------------
        def render_servers():
            for w in server_box.winfo_children():
                w.destroy()
            if not app.servers:
                tk.Label(server_box, text="쓸 수 있는 서버가 없습니다." if app.me else
                         "불러오는 중…", font=F_S, bg=SIDE, fg=MUTED).pack(anchor="w",
                                                                         padx=6)
            for s in app.servers:
                tab = tab_for_server(s["id"])
                on = app.active is not None and app.active.server["id"] == s["id"]
                if tab is not None and tab.phase == "closed":
                    sub, sub_fg, color = "끊김 · 탭에 이유", WARN, WARN_LINE
                elif tab is not None:
                    sub, sub_fg, color = "탭 열림", ACCENT if on else MUTED, ACCENT
                elif s.get("last_check_ok") is False:
                    sub, sub_fg, color = "마지막 확인 실패", WARN, WARN_LINE
                else:
                    sub, sub_fg, color = "누르면 탭으로 열기", MUTED, LINE
                bg = ACCENT_SOFT if on else GROUND
                item = tk.Frame(server_box, bg=bg, cursor="hand2", highlightthickness=1,
                                highlightbackground=ACCENT_LINE if on else LINE)
                item.pack(fill="x", pady=3)
                dot(item, color, bg).pack(side="left", padx=(10, 8))
                words = tk.Frame(item, bg=bg)
                words.pack(side="left", fill="x", expand=True, pady=6)
                tk.Label(words, text=s["name"], font=F_B, bg=bg, fg=INK, anchor="w").pack(
                    fill="x")
                tk.Label(words, text=sub, font=F_S, bg=bg, fg=sub_fg, anchor="w").pack(
                    fill="x")
                bind_tree(item, "<Button-1>", lambda _e, srv=s: open_tab(srv))
            why = block_reason()
            why_lbl.configure(text=why if app.me else "")

        def render_counts():
            n = len(live_tabs())
            tab_pill.configure(text="탭 %d / %d" % (n, app.tab_max),
                               fg=WARN if n >= app.tab_max else MUTED)
            foot_tabs.configure(text="탭 %d개" % n)
            t = app.active
            if t is not None:
                foot_tab.configure(text="%s · %s" % (t.server["name"], {
                    "opening": "여는 중", "closed": "끊김"}.get(
                    t.phase, "열림 " + minutes(t.sess.opened_at if t.sess else None))))
            else:
                foot_tab.configure(text="")

        # --- 새로 읽기 -------------------------------------------------
        def refresh():
            if gen != app.generation:
                return

            def work():
                try:
                    me = app.api.call("GET", "/api/client/me")
                    srv = app.api.call("GET", "/api/client/servers")
                    app.events.put(("refresh", me, srv, None))
                except ApiError as exc:
                    app.events.put(("refresh_err", exc, None, None))
            threading.Thread(target=work, daemon=True).start()
            root.after(REFRESH_SECONDS * 1000, refresh)

        def apply_refresh(me, srv):
            app.me = me
            app.servers = srv["servers"]
            app.can_tunnel = bool(srv.get("can_tunnel"))
            app.has_chat = bool(srv.get("has_chat"))
            app.tab_max = int(me.get("tab_max") or me.get("shell_max") or 4)
            r = me["relay"]
            if r["connected"]:
                relay_pill.configure(text="VDI 중계 붙어 있음 · %s" % r["name"], fg=ACCENT,
                                     bg=ACCENT_SOFT, highlightbackground=ACCENT_LINE)
            else:
                relay_pill.configure(text="VDI 중계가 붙어 있지 않음", fg=WARN, bg=WARN_SOFT,
                                     highlightbackground=WARN_LINE)
            # 탭이 들고 있는 서버 정보(주소 등)를 새로 맞춘다
            for t in app.tabs:
                s = server_by_id(t.server["id"])
                if s is not None:
                    t.server.update({k: v for k, v in s.items() if k != "username"
                                     or not t.server.get("username")})
            render_servers()
            render_center()
            render_targets()
            render_hint()

        # --- Claude 패널 -----------------------------------------------
        target_ids = []

        def render_targets():
            labels = []
            del target_ids[:]
            for s in app.servers:
                tab = tab_for_server(s["id"])
                if app.active is not None and app.active.server["id"] == s["id"]:
                    tail = " · 지금 탭"
                elif tab is not None and tab.phase != "closed":
                    tail = " · 탭"
                else:
                    tail = ""
                labels.append(s["name"] + tail)
                target_ids.append(s["id"])
            target_box.configure(values=labels,
                                 state="readonly" if app.has_chat else "disabled")
            if app.target in target_ids:
                target_box.current(target_ids.index(app.target))
            else:
                target_box.set("")

        def render_hint():
            if ask.get("1.0", "end").strip() or root.focus_get() is ask:
                hint.place_forget()
                return
            s = server_by_id(app.target) if app.target else None
            if not app.has_chat and app.me is not None:
                text = "채팅 메뉴를 쓸 허용이 없습니다"
            elif s is None:
                text = "먼저 위의 「대상」 에서 서버를 고르세요"
            else:
                text = "%s 에 대해 물어보기 · Ctrl+Enter 로 보내기" % s["name"]
            hint.configure(text=text)
            hint.place(x=11, y=8)

        hint.bind("<Button-1>", lambda _e: ask.focus_set())
        ask.bind("<FocusIn>", lambda _e: hint.place_forget())
        ask.bind("<FocusOut>", lambda _e: render_hint())
        ask.bind("<KeyRelease>", lambda _e: render_hint())

        def render_chip():
            shot = app.shot
            if not shot:
                chip_row.pack_forget()
                return
            chip_lbl.configure(text="%s · %d줄 붙임" % (shot["label"], shot["lines"]))
            chip_row.pack(fill="x", padx=16, pady=(12, 0), before=ask_wrap)

        def drop_chip(_e=None):
            app.shot = None
            render_chip()

        chip_x.bind("<Button-1>", drop_chip)

        def set_target(sid, by_user=False):
            if sid == app.target:
                render_targets()
                return
            app.target = sid
            if app.shot and app.shot["server_id"] != sid:
                # 다른 서버의 화면을 이 서버 대화에 보내지 않는다
                drop_chip()
            render_targets()
            render_hint()
            b_send.configure(state="disabled" if sid in app.busy else "normal")
            if sid is None:
                return
            if sid in app.messages:
                render_messages(app.messages[sid])
            else:
                clear_log()
                write("대화를 불러오는 중…\n", "muted")
            load_chat(sid)
            if by_user:
                say("Claude 대상: %s" % (server_by_id(sid) or {}).get("name", ""))

        def on_target(_e=None):
            i = target_box.current()
            if 0 <= i < len(target_ids):
                set_target(target_ids[i], by_user=True)

        target_box.bind("<<ComboboxSelected>>", on_target)

        def clear_log():
            for w in chat.winfo_children():
                w.destroy()
            chat.configure(state="normal")
            chat.delete("1.0", "end")
            chat.configure(state="disabled")

        def write(text, tag="bot"):
            chat.configure(state="normal")
            chat.insert("end", text, tag)
            chat.configure(state="disabled")
            chat.see("end")

        def copy(text):
            root.clipboard_clear()
            root.clipboard_append(text)
            say("클립보드에 넣었습니다. PuTTY 탭에서 Shift+Insert(또는 오른쪽 클릭)로 붙이고 "
                "엔터는 직접 치세요.")

        def card(c, sid):
            """승인 카드. 사람이 누르기 전에는 서버로 나가지 않는다."""
            box = tk.Frame(chat, bg=WARN_SOFT, padx=10, pady=8, highlightthickness=1,
                           highlightbackground=WARN_LINE)
            top_row = tk.Frame(box, bg=WARN_SOFT)
            top_row.pack(fill="x")
            tk.Label(top_row, text=" 변경 · 승인을 기다립니다 ", font=F_S_B, bg=WARN,
                     fg="#FFFFFF").pack(side="left")
            if c.get("expires_in"):
                tk.Label(top_row, text="%d초 안에" % c["expires_in"], font=F_S,
                         bg=WARN_SOFT, fg=MUTED).pack(side="left", padx=8)
            tk.Label(box, text=c["command"], font=MONO_S, bg=WARN_SOFT, fg=INK,
                     wraplength=int(300 * scale), justify="left").pack(anchor="w",
                                                                         pady=6)
            row = tk.Frame(box, bg=WARN_SOFT)
            row.pack(anchor="w")

            def decide(path):
                def work():
                    try:
                        app.api.call("POST", "/api/client/ssh/commands/%d/%s"
                                     % (c["id"], path), {}, timeout=CHAT_TIMEOUT)
                    except ApiError as exc:
                        app.events.put(("say", exc.message, "warn", None))
                    app.events.put(("reload_chat", sid, None, None))
                for b in row.winfo_children():
                    b.configure(state="disabled")
                threading.Thread(target=work, daemon=True).start()

            button(row, "승인", lambda: decide("approve"), "warn", font=F_S_B).pack(
                side="left")
            button(row, "거절", lambda: decide("reject"), font=F_S).pack(side="left",
                                                                        padx=6)
            button(row, "클립보드에", lambda: copy(c["command"]), font=F_S).pack(
                side="left")
            chat.configure(state="normal")
            chat.window_create("end", window=box, padx=2, pady=4)
            chat.insert("end", "\n")
            chat.configure(state="disabled")

        def render_messages(msgs):
            clear_log()
            if not msgs:
                write("아직 대화가 없습니다. 아래에 물어보세요.\n"
                      "탭의 「이 화면을 Claude 에게」 를 누르면 그 화면이 질문에 붙습니다.\n",
                      "muted")
            for m in msgs[-40:]:
                if m["role"] == "user":
                    shot, rest = split_screen(m["content"])
                    write("\n나\n", "me_h")
                    if shot:
                        write(" %s 화면 %d줄 붙음 · %s \n" % (shot["name"], shot["lines"],
                                                          shot["at"]), "chip")
                    write(rest.strip() + "\n", "me")
                    continue
                if m["role"] == "error":
                    write("\n" + m["content"] + "\n", "warn")
                    continue
                write("\nClaude\n", "bot_h")
                write(m["content"].strip() + "\n", "bot")
                for c in m.get("commands") or []:
                    if c["state"] == "pending":
                        card(c, app.target)
                        continue
                    tail = ""
                    if c.get("exit_code") is not None:
                        tail = "  종료 %s" % c["exit_code"]
                    write("[%s · %s] %s%s\n" % (c.get("level_label", c["level"]),
                                                CMD_STATE.get(c["state"], c["state"]),
                                                c["command"], tail), "cmd")
            if app.target in app.busy:
                write("\nClaude 가 서버를 보고 있습니다…\n", "muted")

        def session_for(sid):
            """그 서버의 대화. 없으면 만든다. (작업 스레드에서 부른다)"""
            with app.session_lock:
                if sid in app.sessions:
                    return app.sessions[sid]
            srv = server_by_id(sid) or {"name": str(sid)}
            d = app.api.call("GET", "/api/client/servers/%d/sessions" % sid)
            if d.get("same"):
                sess = d["same"][0]
            else:
                sess = app.api.call("POST", "/api/client/sessions", {
                    "name": "%s · 클라이언트" % srv["name"],
                    "server_id": sid, "visibility": "private"})["session"]
            with app.session_lock:
                app.sessions[sid] = sess
            return sess

        def load_chat(sid):
            if not app.has_chat or sid is None:
                return

            def work():
                try:
                    sess = session_for(sid)
                    d = app.api.call("GET", "/api/client/sessions/%d/messages" % sess["id"])
                    app.events.put(("messages", sid, d["messages"], None))
                except ApiError as exc:
                    app.events.put(("say", exc.message, "warn", None))
            threading.Thread(target=work, daemon=True).start()

        def send():
            sid = app.target
            question = ask.get("1.0", "end").strip()
            shot = app.shot if app.shot and app.shot["server_id"] == sid else None
            if not question and shot:
                question = "이 화면을 봐 줘."
            if not question:
                return
            if sid is None:
                say("먼저 Claude 패널 위의 「대상」 에서 서버를 고르세요.", "warn")
                return
            if sid in app.busy:
                say("이 서버에 대한 앞 질문의 답을 기다리고 있습니다.", "warn")
                return
            text = compose_question(question, shot)
            ask.delete("1.0", "end")
            drop_chip()
            render_hint()
            write("\n나\n", "me_h")
            if shot:
                write(" %s 화면 %d줄 붙음 \n" % (shot["name"], shot["lines"]), "chip")
            write(question + "\n", "me")
            write("\nClaude 가 서버를 보고 있습니다…\n", "muted")
            app.busy.add(sid)
            b_send.configure(state="disabled")

            def work():
                try:
                    sess = session_for(sid)
                    d = app.api.call("POST", "/api/client/sessions/%d/messages" % sess["id"],
                                     {"message": text}, timeout=CHAT_TIMEOUT)
                    if (d.get("ssh") or {}).get("none"):
                        app.events.put(("say", "이 답에서는 서버에 명령을 보내지 않았습니다. "
                                        "서버를 직접 봐야 하면 「서버에서 확인해 줘」 라고 "
                                        "다시 물어 주세요.", "warn", None))
                except ApiError as exc:
                    app.events.put(("say", exc.message, "warn", None))
                app.events.put(("send_done", sid, None, None))
            threading.Thread(target=work, daemon=True).start()

        def send_screen(tab):
            """C-4 B: 누를 때만. PuTTY 의 Copy All → 마지막 60줄 → 칩."""
            if tab.phase != "open" or not tab.hwnd:
                say("열린 탭에서만 화면을 보낼 수 있습니다.", "warn")
                return
            text, note = copy_all(tab.hwnd)
            if text is None:
                say("PuTTY 화면을 읽지 못했습니다. PuTTY 에서 드래그해 복사한 뒤 "
                    "「PuTTY 에서 복사한 것 붙여넣기」 를 쓰세요.", "warn")
                return
            body, n = screen_tail(text)
            if not n:
                say("화면이 비어 있습니다.", "warn")
                return
            set_target(tab.server["id"])
            app.shot = {"server_id": tab.server["id"], "name": tab.server["name"],
                        "label": "%s 화면" % tab.server["name"], "text": body, "lines": n,
                        "at": time.strftime("%H:%M:%S")}
            render_chip()
            focus_ask()
            say(note or "%s 화면 %d줄을 붙였습니다. 물어볼 것을 적고 보내세요. 보내기 전에 × 로 "
                        "뺄 수 있습니다." % (tab.server["name"], n), "warn" if note else "info")

        def paste_clip():
            sid = app.target
            if sid is None:
                say("먼저 「대상」 에서 서버를 고르세요.", "warn")
                return
            try:
                clip = root.clipboard_get()
            except tk.TclError:
                clip = ""
            body, n = screen_tail(clip)
            if not n:
                say("클립보드가 비어 있습니다. PuTTY 에서 드래그하면 바로 복사됩니다.", "warn")
                return
            name = (server_by_id(sid) or {}).get("name", "")
            app.shot = {"server_id": sid, "name": name, "label": "복사한 것", "text": body,
                        "lines": n, "at": time.strftime("%H:%M:%S")}
            render_chip()
            focus_ask()

        def focus_ask():
            if focused_window() in focus.hosts:
                set_focus(toplevel_of(root.winfo_id()))
            ask.focus_set()
            hint.place_forget()

        b_send.configure(command=send)
        b_paste.configure(command=paste_clip)
        ask.bind("<Control-Return>", lambda _e: (send(), "break")[1])
        root.bind("<Control-V>", lambda _e: paste_clip())   # Ctrl+Shift+V

        # --- 이벤트 펌프 -----------------------------------------------
        def pump():
            if gen != app.generation:
                return
            try:
                while True:
                    ev = app.events.get_nowait()
                    kind = ev[0]
                    # 사건 하나가 실패해도 펌프는 멈추지 않는다. 멈추면 그 뒤의 사건
                    # (PuTTY 를 탭에 넣기, 열림, 끊김)이 전부 쌓이기만 하고 화면이 굳는다.
                    try:
                        if kind == "refresh":
                            apply_refresh(ev[1], ev[2])
                        elif kind == "refresh_err":
                            exc = ev[1]
                            if exc.status == 401:
                                show_register(exc.message)
                                return
                            say(exc.message, "warn")
                        elif kind == "say":
                            say(ev[1], ev[2])
                        elif kind == "started":
                            tab, sess = ev[1], ev[2]
                            if tab in app.tabs:
                                tab.sess = sess
                            else:
                                threading.Thread(target=sess.close, daemon=True).start()
                        elif kind == "open_failed":
                            tab = ev[1]
                            if tab in app.tabs:
                                tab.phase = "closed"
                                tab.reason = ev[2]
                                tab.show_body()
                                render_center()
                                render_servers()
                                render_targets()
                            say(ev[2], "warn")
                        elif kind == "launched":
                            tab = tab_for_session(ev[1])
                            if tab is not None:
                                tab.launched = True
                                tab.show_body()
                            threading.Thread(target=find_window, args=(ev[1],),
                                             daemon=True).start()
                        elif kind == "found":
                            on_found(ev[1], ev[2])
                        elif kind == "tunnel":
                            tab = tab_for_session(ev[1])
                            if tab is not None and ev[1].state == "열림":
                                tab.phase = "open"
                                tab.show_body()
                            say(ev[2], ev[3])
                            render_center()
                            render_servers()
                            render_targets()
                        elif kind == "closed":
                            sess = ev[1]
                            tab = tab_for_session(sess)
                            if tab is not None:
                                # 끊긴 탭은 저절로 닫히지 않는다. 이유와 「다시 열기」 가 남는다.
                                tab.phase = "closed"
                                tab.reason = ev[2]
                                tab.forget_window()
                                tab.show_body()
                                say("%s · %s" % (sess.server["name"], ev[2]), "warn")
                            render_center()
                            render_servers()
                            render_targets()
                        elif kind == "messages":
                            app.messages[ev[1]] = ev[2]
                            if ev[1] == app.target:
                                render_messages(ev[2])
                        elif kind == "reload_chat":
                            load_chat(ev[1])
                        elif kind == "send_done":
                            app.busy.discard(ev[1])
                            if ev[1] == app.target:
                                b_send.configure(state="normal")
                            load_chat(ev[1])
                    except Exception:          # noqa: BLE001
                        import traceback
                        log("사건 %s 처리 실패: %s" % (kind, traceback.format_exc().rstrip()))
                        say("오류가 났습니다. %s 를 보내 주세요." % LOG_PATH, "warn")
            except queue.Empty:
                pass
            root.after(100, pump)

        def tick():
            """시간 표시를 새로 하고, 붙기 전에 꺼진 PuTTY 를 알아챈다."""
            if gen != app.generation:
                return
            for t in app.tabs:
                s = t.sess
                proc = getattr(s, "proc", None) if s else None
                if (s is not None and not s.stop.is_set() and proc is not None
                        and hasattr(proc, "poll") and proc.poll() is not None):
                    threading.Thread(target=s.finish, args=("PuTTY 가 닫혔습니다",),
                                     kwargs={"close_remote": True}, daemon=True).start()
                t.paint()
            render_counts()
            root.after(5000, tick)

        def watch_dialogs():
            """
            PuTTY 가 대화상자를 띄우면 앞으로 꺼낸다. 처음 붙는 서버는 반드시 호스트 키를
            묻는다(「PuTTY Security Alert」). 그 창이 뒤에 깔리면 탭이 멈춘 것처럼 보인다.
            """
            if gen != app.generation:
                return
            try:
                top = toplevel_of(root.winfo_id())
                for t in app.tabs:
                    pid = getattr(getattr(t.sess, "proc", None), "pid", None)
                    if t.phase == "closed" or not pid:
                        if t.dialog:
                            t.dialog = None
                            t.paint()
                        continue
                    found = find_putty_dialogs(pid)
                    now = found[0] if found else None
                    if now and now != t.dialog:
                        log("탭 %s PuTTY 대화상자 %s" % (t.server["name"], now))
                        if app.active is not t:
                            activate(t)
                        bring_dialog(now, top)
                        say("%s · PuTTY 가 확인을 기다립니다 (처음 붙는 서버면 호스트 키). "
                            "앞에 뜬 창에서 고르세요." % t.server["name"], "warn")
                    elif not now and t.dialog:
                        # 대답했다 → 탭을 다시 그리고 PuTTY 에 키보드를 준다
                        t.fit()
                        if app.active is t and t.embedded and t.hwnd:
                            focus.give(t.hwnd)
                    if now != t.dialog:
                        t.dialog = now
                        t.paint()
            except Exception:                  # noqa: BLE001
                import traceback
                log("대화상자 감시 실패: %s" % traceback.format_exc().rstrip())
            root.after(600, watch_dialogs)

        def close_everything():
            for t in list(app.tabs):
                if t.sess is not None:
                    t.sess.kill_putty()
                t.forget_window()
            for t in list(app.tabs):
                if t.sess is not None:
                    t.sess.close("클라이언트를 닫았습니다")

        app.close_all = close_everything
        say("putty.exe : %s" % (putty_path(cfg) or "없음 — 이 프로그램 옆에 두세요"))
        render_servers()
        render_center()
        render_hint()
        pump()
        refresh()
        tick()
        watch_dialogs()

    # --- C-7 설정 ---------------------------------------------------------
    def show_settings():
        cfg = state["cfg"]
        win = tk.Toplevel(root)
        win.title("설정")
        win.configure(bg=GROUND)
        win.geometry("%dx%d" % (680 * scale, 400 * scale))
        f = tk.Frame(win, bg=GROUND, padx=20, pady=20)
        f.pack(fill="both", expand=True)
        rows = [("챗봇 서버", cfg.get("url", "")),
                ("설정 파일", CONFIG_PATH),
                ("이 프로그램", here()),
                ("putty.exe", putty_path(cfg) or "없음"),
                ("PuTTY 탭", "한 사람 %s개까지 (관리자가 웹의 중계 설정에서 바꿉니다)"
                 % (getattr(app, "tab_max", None) or "-")),
                ("탭 안에 넣기", "켜짐" if app.embed else "이 PC 에서는 별도 창으로"),
                ("등록한 때", cfg.get("registered_at", ""))]
        for i, (k, v) in enumerate(rows):
            tk.Label(f, text=k, bg=GROUND, fg=MUTED).grid(row=i, column=0, sticky="w", pady=3)
            tk.Label(f, text=v, font=MONO, bg=GROUND, fg=INK).grid(row=i, column=1,
                                                                   sticky="w", padx=12)
        loghost = tk.BooleanVar(value=cfg.get("use_loghost", True))

        def save_loghost():
            cfg["use_loghost"] = loghost.get()
            save_config(cfg)

        tk.Checkbutton(f, variable=loghost, command=save_loghost, bg=GROUND,
                       activebackground=GROUND, selectcolor=GROUND,
                       text="PuTTY 제목과 호스트 키를 서버 이름으로 (-loghost). "
                            "PuTTY 가 이 옵션을 모르면 끄세요").grid(
            row=len(rows), column=0, columnspan=2, sticky="w", pady=(14, 4))

        def pick_putty():
            path = filedialog.askopenfilename(title="putty.exe 고르기",
                                              filetypes=[("putty.exe", "putty.exe")])
            if path:
                cfg["putty_path"] = path
                save_config(cfg)
                win.destroy()

        def forget():
            # 이 PC 의 키만 지운다. 서버 쪽 등록은 웹의 「등록 해제」 가 끊는다.
            close_all()
            try:
                os.remove(CONFIG_PATH)
            except OSError:
                pass
            state["cfg"] = {"url": cfg.get("url", "")}
            win.destroy()
            show_register("이 PC 의 키를 지웠습니다. 웹에서도 「등록 해제」 를 눌러 "
                          "서버 쪽 등록을 끊으세요.")

        btns = tk.Frame(f, bg=GROUND)
        btns.grid(row=len(rows) + 1, column=0, columnspan=2, sticky="w", pady=12)
        button(btns, "putty.exe 직접 고르기", pick_putty).pack(side="left")
        button(btns, "이 PC 의 등록 지우기", forget).pack(side="left", padx=8)

    def close_all():
        fn = getattr(app, "close_all", None)
        if fn:
            fn()

    def on_close():
        log("창을 닫음")
        close_all()
        focus.close()
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_close)
    if state["cfg"] and state["cfg"].get("client_key"):
        show_main()
    else:
        show_register()
    root.mainloop()


def main(argv):
    if len(argv) > 1 and argv[1] in ("-h", "--help", "help"):
        print(__doc__)
        return 0
    if len(argv) > 1 and argv[1] == "--version":
        print(VERSION)
        return 0
    start_log()
    try:
        run_gui()
    except BaseException:
        import traceback
        log("run_gui 가 끝남: %s" % traceback.format_exc().rstrip())
        raise
    log("정상 종료")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
