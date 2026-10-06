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
    3. PuTTY 띄우기  그 포트로 붙는 putty.exe 를 띄운다. 터미널은 PuTTY 가 그린다
    4. Claude 패널   웹 채팅과 같은 대화. claude -p 는 챗봇 서버에서 돈다

이 프로그램이 하지 않는 일

    - 대상 서버의 비밀번호를 묻거나 저장하지 않는다. PuTTY 창에서 직접 친다
    - SSH 를 하지 않는다. 바이트만 옮긴다(암호문이라 읽을 수도 없다)
    - Claude 가 PuTTY 에 대신 타이핑하지 않는다. 명령은 클립보드까지만 간다
    - 0.0.0.0 에 포트를 열지 않는다. 127.0.0.1 만, 연결 하나만 받는다

의존성
------
표준 라이브러리만 쓴다 (tkinter, urllib, http.client, socket, threading).
putty.exe 는 이 프로그램과 **같은 폴더**에 둔다.

    pyinstaller --onefile --windowed --name claude-term claude_term.py
"""

import base64
import http.client
import json
import os
import queue
import socket
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

VERSION = "1.0.0"
APP_NAME = "Claude 터미널"

APP_DIR = os.path.join(
    os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "claude-term")
CONFIG_PATH = os.path.join(APP_DIR, "client.json")

HTTP_TIMEOUT = 30
CHAT_TIMEOUT = 330          # 서버 쪽 채팅 예산(240초)보다 넉넉하게
REFRESH_SECONDS = 10
ACCEPT_SECONDS = 60         # PuTTY 가 붙기를 기다리는 시간


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
# 터널 하나
# ---------------------------------------------------------------------------
class TunnelSession(object):
    """
    터널 하나와 그 위의 PuTTY 하나.

    events 큐로 화면에 소식을 보낸다. 화면(tkinter)은 자기 스레드에서만
    만질 수 있으므로 여기서 직접 그리지 않는다.
    """

    def __init__(self, app, server, info):
        self.app = app
        self.server = server
        self.id = info["tunnel_id"]
        self.stop = threading.Event()
        self.opened = threading.Event()
        self.state = "여는 중"
        self.reason = ""
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
        argv = [exe, "-ssh", "-P", str(self.port), "-l", self.server["username"]]
        if self.app.cfg.get("use_loghost", True):
            # 제목과 호스트 키를 127.0.0.1 이 아니라 서버 이름으로 기억하게 한다.
            argv += ["-loghost", self.server["name"]]
        argv.append("127.0.0.1")
        try:
            self.proc = subprocess.Popen(argv, close_fds=True)
        except OSError as exc:
            self.finish("PuTTY 를 띄우지 못했습니다 (%s)" % exc, close_remote=True)
            return
        self.emit("%s · PuTTY 를 띄웠습니다 (127.0.0.1:%d)" % (self.server["name"], self.port))
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
        self.emit("%s · 터널 열림. PuTTY 창에서 비밀번호를 치세요" % self.server["name"])
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
                self.finish("PuTTY 창을 닫았습니다", close_remote=True)
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

    def finish(self, reason, close_remote=False):
        if self.stop.is_set():
            return
        self.stop.set()
        self.state = "닫힘"
        self.reason = reason
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
def run_gui():
    import tkinter as tk
    from tkinter import filedialog, ttk

    INK = "#16231F"
    MUTED = "#5A6B65"
    LINE = "#CBD4D0"
    ACCENT = "#1C6A58"
    WARN = "#A8491A"
    GROUND = "#FFFFFF"
    PANEL = "#F4F6F5"

    root = tk.Tk()
    root.title("%s %s" % (APP_NAME, VERSION))
    root.geometry("1180x720")
    root.minsize(900, 560)
    root.configure(bg=GROUND)
    font = ("Malgun Gothic", 10)
    mono = ("Consolas", 10)
    root.option_add("*Font", font)

    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except tk.TclError:
        pass
    style.configure("TFrame", background=GROUND)
    style.configure("Panel.TFrame", background=PANEL)
    style.configure("TLabel", background=GROUND, foreground=INK)
    style.configure("Muted.TLabel", background=GROUND, foreground=MUTED)
    style.configure("Panel.TLabel", background=PANEL, foreground=INK)
    style.configure("Warn.TLabel", background=GROUND, foreground=WARN)
    style.configure("Title.TLabel", background=GROUND, foreground=INK,
                    font=("Malgun Gothic", 15, "bold"))
    style.configure("Accent.TButton", foreground="#FFFFFF", background=ACCENT)
    style.map("Accent.TButton", background=[("active", "#124437"), ("disabled", LINE)])
    style.configure("Treeview", rowheight=26)

    state = {"cfg": load_config(), "api": None, "frame": None}

    class App(object):
        pass

    app = App()
    app.events = queue.Queue()
    app.tunnels = {}                       # tunnel_id -> TunnelSession
    app.servers = []
    app.me = None
    app.session = None                     # 지금 Claude 패널의 대화
    app.session_server = None

    def clear_root():
        if state["frame"] is not None:
            state["frame"].destroy()

    # 화면이 바뀌면 앞 화면의 새로 읽기/이벤트 펌프가 멈춰야 한다. 세대 번호로 가린다.
    app.generation = 0

    # --- C-1 등록 --------------------------------------------------------
    def show_register(message=""):
        app.generation += 1
        clear_root()
        f = ttk.Frame(root, padding=36)
        f.pack(fill="both", expand=True)
        state["frame"] = f
        ttk.Label(f, text="처음 한 번만 등록합니다", style="Title.TLabel").pack(anchor="w")
        ttk.Label(f, style="Muted.TLabel", wraplength=760, justify="left",
                  text="웹의 서버 화면에서 「내 클라이언트」 코드를 받아 넣으면 이 PC 가 "
                       "내 클라이언트가 됩니다. 남의 코드로는 등록되지 않습니다.").pack(
            anchor="w", pady=(4, 18))

        row = ttk.Frame(f)
        row.pack(fill="x")
        ttk.Label(row, text="챗봇 서버 주소").grid(row=0, column=0, sticky="w")
        ttk.Label(row, text="등록 코드").grid(row=0, column=1, sticky="w", padx=(14, 0))
        url = tk.StringVar(value=(state["cfg"] or {}).get("url", "https://"))
        code = tk.StringVar()
        insecure = tk.BooleanVar(value=bool((state["cfg"] or {}).get("insecure")))
        e_url = ttk.Entry(row, textvariable=url, width=52, font=mono)
        e_url.grid(row=1, column=0, sticky="we", pady=4)
        e_code = ttk.Entry(row, textvariable=code, width=12, font=("Consolas", 13))
        e_code.grid(row=1, column=1, sticky="w", padx=(14, 0), pady=4)
        btn = ttk.Button(row, text="등록", style="Accent.TButton")
        btn.grid(row=1, column=2, padx=(14, 0))
        ttk.Checkbutton(f, text="사내 사설 인증서 (인증서 검사를 하지 않음)",
                        variable=insecure).pack(anchor="w", pady=(6, 0))

        msg = ttk.Label(f, text=message, style="Warn.TLabel", wraplength=760,
                        justify="left")
        msg.pack(anchor="w", pady=(14, 0))

        notes = ("1   코드는 10분 뒤에 만료됩니다. 새로 받으면 전에 받은 코드만 죽습니다.\n"
                 "2   등록하면 키가 %s 에 저장됩니다. 대상 서버 비밀번호는 저장하지 않습니다.\n"
                 "3   putty.exe 는 이 프로그램과 같은 폴더에 있어야 합니다. 지금: %s"
                 % (CONFIG_PATH, putty_path(state["cfg"]) or "없음"))
        ttk.Label(f, text=notes, style="Muted.TLabel", justify="left").pack(
            anchor="w", pady=(18, 0))

        def do_register():
            u = url.get().strip()
            if not u.startswith(("http://", "https://")):
                u = "https://" + u
            api = Api(u, insecure=insecure.get())
            btn.state(["disabled"])
            msg.configure(text="등록하는 중...")

            def work():
                try:
                    res = api.call("POST", "/api/client/register", {
                        "code": code.get().strip(),
                        "name": os.environ.get("COMPUTERNAME") or socket.gethostname(),
                        "version": VERSION, "os": "%s %s" % (os.name, sys.platform)})
                except ApiError as exc:
                    text = exc.message      # exc 는 except 블록이 끝나면 사라진다
                    root.after(0, lambda: (btn.state(["!disabled"]),
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

        outer = ttk.Frame(root)
        outer.pack(fill="both", expand=True)
        state["frame"] = outer

        top = ttk.Frame(outer, padding=(16, 10))
        top.pack(fill="x")
        ttk.Label(top, text=APP_NAME, font=("Malgun Gothic", 11, "bold")).pack(side="left")
        ttk.Label(top, text=VERSION, style="Muted.TLabel", font=mono).pack(side="left",
                                                                         padx=8)
        relay_lbl = ttk.Label(top, text="", style="Muted.TLabel")
        relay_lbl.pack(side="left", padx=14)
        shell_lbl = ttk.Label(top, text="", style="Muted.TLabel")
        shell_lbl.pack(side="left", padx=6)
        ttk.Button(top, text="설정", command=lambda: show_settings()).pack(side="right")

        body = ttk.Frame(outer)
        body.pack(fill="both", expand=True)

        # 왼쪽: 서버 목록
        left = ttk.Frame(body, padding=(16, 4, 8, 8))
        left.pack(side="left", fill="both", expand=True)
        ttk.Label(left, text="내가 쓸 수 있는 서버", font=("Malgun Gothic", 11, "bold")).pack(
            anchor="w")
        ttk.Label(left, text="웹에서 관리자가 등록한 것. 여기서는 고르기만 합니다",
                  style="Muted.TLabel").pack(anchor="w", pady=(0, 6))
        tree = ttk.Treeview(left, columns=("addr", "auth", "state"), show="tree headings",
                            selectmode="browse", height=12)
        tree.heading("#0", text="서버")
        tree.heading("addr", text="주소")
        tree.heading("auth", text="인증")
        tree.heading("state", text="상태")
        tree.column("#0", width=150)
        tree.column("addr", width=230)
        tree.column("auth", width=70)
        tree.column("state", width=170)
        tree.pack(fill="both", expand=True)

        bar = ttk.Frame(left)
        bar.pack(fill="x", pady=8)
        b_open = ttk.Button(bar, text="PuTTY 열기", style="Accent.TButton")
        b_open.pack(side="left")
        b_close = ttk.Button(bar, text="터널 닫기")
        b_close.pack(side="left", padx=6)
        b_chat = ttk.Button(bar, text="이 서버로 Claude")
        b_chat.pack(side="left")
        why_lbl = ttk.Label(left, text="", style="Warn.TLabel", wraplength=560,
                            justify="left")
        why_lbl.pack(anchor="w")
        ttk.Label(left, style="Muted.TLabel", wraplength=560, justify="left",
                  text="PuTTY 는 별도 창으로 뜹니다. 이 프로그램 안에 터미널을 그리지 "
                       "않습니다. 비밀번호는 PuTTY 창에서 직접 칩니다 — 이 프로그램도 "
                       "챗봇 서버도 그 글자를 보지 못합니다.").pack(anchor="w", pady=(8, 0))

        # 오른쪽: Claude 패널 (C-3)
        right = ttk.Frame(body, style="Panel.TFrame", padding=12)
        right.pack(side="right", fill="both", expand=True)
        head = ttk.Frame(right, style="Panel.TFrame")
        head.pack(fill="x")
        ttk.Label(head, text="Claude", style="Panel.TLabel",
                  font=("Malgun Gothic", 12, "bold")).pack(side="left")
        chat_srv = ttk.Label(head, text="서버를 고르고 「이 서버로 Claude」", style="Panel.TLabel")
        chat_srv.pack(side="left", padx=10)

        log = tk.Text(right, wrap="word", height=10, bg=GROUND, fg=INK, relief="flat",
                      font=font, padx=10, pady=8, state="disabled",
                      highlightthickness=1, highlightbackground=LINE)
        log.pack(fill="both", expand=True, pady=8)
        log.tag_configure("me", foreground=ACCENT, font=("Malgun Gothic", 10, "bold"))
        log.tag_configure("bot", foreground=INK)
        log.tag_configure("cmd", foreground=MUTED, font=mono)
        log.tag_configure("warn", foreground=WARN)
        log.tag_configure("muted", foreground=MUTED)

        cards = ttk.Frame(right, style="Panel.TFrame")
        cards.pack(fill="x")

        ttk.Button(right, text="PuTTY 에서 복사한 것을 붙여넣기  ·  Ctrl+Shift+V",
                   command=lambda: paste_clip()).pack(fill="x", pady=(6, 4))
        ask_row = ttk.Frame(right, style="Panel.TFrame")
        ask_row.pack(fill="x")
        ask = tk.Text(ask_row, height=3, wrap="word", font=font, relief="flat",
                      highlightthickness=1, highlightbackground=LINE)
        ask.pack(side="left", fill="x", expand=True)
        b_send = ttk.Button(ask_row, text="보내기", style="Accent.TButton")
        b_send.pack(side="left", padx=(8, 0), fill="y")

        status = ttk.Label(outer, text="", style="Muted.TLabel", padding=(16, 6))
        status.pack(fill="x", side="bottom")

        # --- 도우미 -----------------------------------------------------
        def say(text, level="info"):
            status.configure(text=text, style=("Warn.TLabel" if level == "warn"
                                               else "Muted.TLabel"))

        def selected():
            sel = tree.selection()
            if not sel:
                return None
            for s in app.servers:
                if str(s["id"]) == sel[0]:
                    return s
            return None

        def open_for(server):
            for t in app.tunnels.values():
                if t.server["id"] == server["id"] and not t.stop.is_set():
                    return t
            return None

        def render_servers():
            keep = tree.selection()
            tree.delete(*tree.get_children())
            for s in app.servers:
                t = open_for(s)
                st = ("터널 " + t.state) if t else (
                    "마지막 확인 실패" if s.get("last_check_ok") is False else "켜짐")
                tree.insert("", "end", iid=str(s["id"]), text=s["name"],
                            values=(s["address"], s["auth_label"], st))
            if keep and tree.exists(keep[0]):
                tree.selection_set(keep[0])
            update_buttons()

        def block_reason(server):
            """누를 수 없는 단추에는 이유를 붙인다. 막는 것은 서버다(눌러도 서버가 거절)."""
            if server is None:
                return "서버를 고르세요."
            if app.me and not app.me.get("version_ok", True):
                return ("이 클라이언트가 낡았습니다. 서버가 %s 이상을 요구합니다. 웹에서 "
                        "새로 받아 주세요." % app.me.get("min_version"))
            if not app.can_tunnel:
                return "PuTTY 터널을 열 허용이 없습니다. 관리자에게 요청하세요."
            if app.me and not app.me["relay"]["connected"]:
                return ("내 VDI 의 중계 프로그램이 붙어 있지 않습니다. 웹의 서버 화면 → "
                        "「내 중계」 에서 설치하고 등록 코드를 넣으세요.")
            if not putty_path(app.cfg):
                return ("putty.exe 를 찾지 못했습니다. 이 프로그램과 같은 폴더(%s)에 "
                        "두세요." % here())
            return ""

        def update_buttons():
            s = selected()
            t = open_for(s) if s else None
            why = "" if t else block_reason(s)
            b_open.state(["disabled"] if (why or t) else ["!disabled"])
            b_close.state(["!disabled"] if t else ["disabled"])
            b_chat.state(["!disabled"] if (s and app.has_chat) else ["disabled"])
            why_lbl.configure(text=why if s else "")

        tree.bind("<<TreeviewSelect>>", lambda _e: update_buttons())

        # --- 새로 읽기 -------------------------------------------------
        app.can_tunnel = False
        app.has_chat = False

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
            r = me["relay"]
            relay_lbl.configure(
                text=("VDI 중계 붙어 있음 · %s" % r["name"]) if r["connected"]
                else "VDI 중계가 붙어 있지 않음",
                style="Muted.TLabel" if r["connected"] else "Warn.TLabel")
            shell_lbl.configure(text="셸 %d / %d" % (me["shell_open"], me["shell_max"]))
            render_servers()

        # --- 터널 -----------------------------------------------------
        def open_tunnel():
            s = selected()
            if s is None or open_for(s):
                return
            why = block_reason(s)
            if why:
                say(why, "warn")
                return
            b_open.state(["disabled"])
            say("%s · 터널을 여는 중..." % s["name"])

            def work():
                try:
                    info = app.api.call("POST", "/api/client/tunnel", {"server_id": s["id"]})
                except ApiError as exc:
                    app.events.put(("say", exc.message, "warn", None))
                    app.events.put(("render", None, None, None))
                    return
                t = TunnelSession(app, s, info)
                app.tunnels[t.id] = t
                t.start()
                app.events.put(("render", None, None, None))
            threading.Thread(target=work, daemon=True).start()

        def close_tunnel():
            s = selected()
            t = open_for(s) if s else None
            if t:
                threading.Thread(target=t.close, daemon=True).start()

        b_open.configure(command=open_tunnel)
        b_close.configure(command=close_tunnel)
        tree.bind("<Double-1>", lambda _e: open_tunnel())

        # --- Claude 패널 ---------------------------------------------
        def write(text, tag="bot"):
            log.configure(state="normal")
            log.insert("end", text, tag)
            log.configure(state="disabled")
            log.see("end")

        def clear_cards():
            for w in cards.winfo_children():
                w.destroy()

        def copy(text):
            root.clipboard_clear()
            root.clipboard_append(text)
            say("클립보드에 넣었습니다. PuTTY 창에서 Shift+Insert 로 붙이고 엔터는 직접 "
                "치세요.")

        def card(c):
            """승인 카드. 사람이 누르기 전에는 서버로 나가지 않는다."""
            box = ttk.Frame(cards, style="Panel.TFrame", padding=(0, 4))
            box.pack(fill="x")
            ttk.Label(box, text="변경 · 승인을 기다립니다", style="Warn.TLabel").pack(anchor="w")
            ttk.Label(box, text=c["command"], font=mono, style="Panel.TLabel",
                      wraplength=480, justify="left").pack(anchor="w")
            row = ttk.Frame(box, style="Panel.TFrame")
            row.pack(anchor="w", pady=2)

            def decide(path):
                def work():
                    try:
                        app.api.call("POST", "/api/client/ssh/commands/%d/%s"
                                     % (c["id"], path), {}, timeout=CHAT_TIMEOUT)
                    except ApiError as exc:
                        app.events.put(("say", exc.message, "warn", None))
                    app.events.put(("reload_chat", None, None, None))
                threading.Thread(target=work, daemon=True).start()

            ttk.Button(row, text="승인", style="Accent.TButton",
                       command=lambda: decide("approve")).pack(side="left")
            ttk.Button(row, text="거절", command=lambda: decide("reject")).pack(
                side="left", padx=4)
            ttk.Button(row, text="클립보드에 넣기",
                       command=lambda: copy(c["command"])).pack(side="left")

        def render_messages(msgs):
            log.configure(state="normal")
            log.delete("1.0", "end")
            log.configure(state="disabled")
            clear_cards()
            for m in msgs[-40:]:
                if m["role"] == "user":
                    write("\n나  ", "me")
                    write(m["content"] + "\n", "bot")
                    continue
                if m["role"] == "error":
                    write("\n" + m["content"] + "\n", "warn")
                    continue
                write("\nClaude  ", "me")
                write(m["content"] + "\n", "bot")
                for c in m.get("commands") or []:
                    if c["state"] == "pending":
                        card(c)
                    else:
                        write("  [%s · %s] %s\n" % (c.get("level_label", c["level"]),
                                                   c["state"], c["command"]), "cmd")

        def load_chat():
            s = app.session
            if not s:
                return

            def work():
                try:
                    d = app.api.call("GET", "/api/client/sessions/%d/messages" % s["id"])
                    app.events.put(("messages", d["messages"], None, None))
                except ApiError as exc:
                    app.events.put(("say", exc.message, "warn", None))
            threading.Thread(target=work, daemon=True).start()

        def start_chat():
            s = selected()
            if s is None:
                return
            chat_srv.configure(text=s["name"])
            say("%s · 대화를 찾는 중..." % s["name"])

            def work():
                try:
                    d = app.api.call("GET", "/api/client/servers/%d/sessions" % s["id"])
                    if d.get("same"):
                        sess = d["same"][0]
                    else:
                        sess = app.api.call("POST", "/api/client/sessions", {
                            "name": "%s · 클라이언트" % s["name"],
                            "server_id": s["id"], "visibility": "private"})["session"]
                    app.session = sess
                    app.session_server = s
                    app.events.put(("say", "%s · 대화 「%s」. 웹에서도 이어서 볼 수 있습니다"
                                    % (s["name"], sess["name"]), "info", None))
                    load_chat()
                except ApiError as exc:
                    app.events.put(("say", exc.message, "warn", None))
            threading.Thread(target=work, daemon=True).start()

        def send():
            text = ask.get("1.0", "end").strip()
            if not text:
                return
            if not app.session:
                say("먼저 서버를 고르고 「이 서버로 Claude」 를 누르세요.", "warn")
                return
            ask.delete("1.0", "end")
            write("\n나  ", "me")
            write(text + "\n", "bot")
            write("Claude 가 서버를 보고 있습니다...\n", "muted")
            b_send.state(["disabled"])
            sid = app.session["id"]

            def work():
                try:
                    d = app.api.call("POST", "/api/client/sessions/%d/messages" % sid,
                                     {"message": text}, timeout=CHAT_TIMEOUT)
                    if (d.get("ssh") or {}).get("none"):
                        app.events.put(("say", "이 답에서는 서버에 명령을 보내지 않았습니다. "
                                        "서버를 직접 봐야 하면 「서버에서 확인해 줘」 라고 "
                                        "다시 물어 주세요.", "warn", None))
                except ApiError as exc:
                    app.events.put(("say", exc.message, "warn", None))
                app.events.put(("reload_chat", None, None, None))
                app.events.put(("send_done", None, None, None))
            threading.Thread(target=work, daemon=True).start()

        def paste_clip():
            try:
                clip = root.clipboard_get()
            except tk.TclError:
                say("클립보드가 비어 있습니다. PuTTY 에서 드래그하면 바로 복사됩니다.", "warn")
                return
            # 언어 표시 없는 블록으로 감싼다. 챗봇은 이런 블록을 명령으로 실행하지 않는다.
            ask.insert("end", "PuTTY 화면:\n```\n%s\n```\n" % clip.rstrip())
            ask.focus_set()

        b_chat.configure(command=start_chat)
        b_send.configure(command=send)
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
                    elif kind == "render":
                        render_servers()
                    elif kind == "tunnel":
                        say(ev[2], ev[3])
                        render_servers()
                    elif kind == "closed":
                        t = ev[1]
                        app.tunnels.pop(t.id, None)
                        # PuTTY 는 「Network error」 밖에 모른다. 이유는 여기서 말한다.
                        say("%s · %s" % (t.server["name"], ev[2]), "warn")
                        render_servers()
                    elif kind == "messages":
                        render_messages(ev[1])
                    elif kind == "reload_chat":
                        load_chat()
                    elif kind == "send_done":
                        b_send.state(["!disabled"])
            except queue.Empty:
                pass
            root.after(120, pump)

        say("putty.exe : %s" % (putty_path(cfg) or "없음 — 이 프로그램 옆에 두세요"))
        pump()
        refresh()

    # --- C-7 설정 ---------------------------------------------------------
    def show_settings():
        cfg = state["cfg"]
        win = tk.Toplevel(root)
        win.title("설정")
        win.configure(bg=GROUND)
        win.geometry("640x380")
        f = ttk.Frame(win, padding=20)
        f.pack(fill="both", expand=True)
        rows = [("챗봇 서버", cfg.get("url", "")),
                ("설정 파일", CONFIG_PATH),
                ("이 프로그램", here()),
                ("putty.exe", putty_path(cfg) or "없음"),
                ("등록한 때", cfg.get("registered_at", ""))]
        for i, (k, v) in enumerate(rows):
            ttk.Label(f, text=k, style="Muted.TLabel").grid(row=i, column=0, sticky="w",
                                                           pady=3)
            ttk.Label(f, text=v, font=mono).grid(row=i, column=1, sticky="w", padx=12)
        loghost = tk.BooleanVar(value=cfg.get("use_loghost", True))

        def save_loghost():
            cfg["use_loghost"] = loghost.get()
            save_config(cfg)

        ttk.Checkbutton(f, variable=loghost, command=save_loghost,
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
            try:
                os.remove(CONFIG_PATH)
            except OSError:
                pass
            state["cfg"] = {"url": cfg.get("url", "")}
            win.destroy()
            show_register("이 PC 의 키를 지웠습니다. 웹에서도 「등록 해제」 를 눌러 "
                          "서버 쪽 등록을 끊으세요.")

        btns = ttk.Frame(f)
        btns.grid(row=len(rows) + 1, column=0, columnspan=2, sticky="w", pady=12)
        ttk.Button(btns, text="putty.exe 직접 고르기", command=pick_putty).pack(side="left")
        ttk.Button(btns, text="이 PC 의 등록 지우기", command=forget).pack(side="left",
                                                                     padx=8)

    def on_close():
        for t in list(getattr(app, "tunnels", {}).values()):
            t.close("클라이언트를 닫았습니다")
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
    run_gui()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
