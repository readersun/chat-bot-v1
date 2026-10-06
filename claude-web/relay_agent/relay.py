#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
relay - 사내망 SSH 중계 (VDI 에서 돈다)
=======================================

챗봇 서버는 사내망 리눅스 서버에 직접 붙지 못한다. 이 프로그램이 VDI 에서
돌면서 **챗봇 서버로 들어가는 방향 하나만** 써서 할 일을 받아 오고 결과를
올린다. 그래서 방화벽에 구멍을 내지 않는다.

    relay.exe register     서버 주소와 등록 코드를 한 번 적는다
    relay.exe status       지금 설정과 연결 상태를 본다
    relay.exe run          계속 돈다 (인자 없이 실행하면 이것)
    relay.exe unregister   이 PC 의 설정을 지운다

화면이 없다. 웹이 조종하는 PuTTY 한 대다.

하는 일
-------
    - 챗봇 서버에서 할 일을 받아 ssh.exe / plink.exe 를 돌린다
    - 터미널 화면을 그대로 올려 보낸다
    - 받은 접속 정보는 메모리에만 두고 쓰고 버린다
    - PuTTY 터널: 대상 서버의 22번에 **소켓만** 열고 바이트를 옮긴다.
      SSH 는 사용자 PC 의 PuTTY 가 한다. 이 프로그램은 암호문만 본다.

안 하는 일
----------
    - 접속 정보를 디스크에 적지 않는다 (이 PC 에 남는 것은 중계 토큰 하나뿐)
    - 명령을 스스로 만들지 않는다. 받은 것만 그대로 돌린다
    - 챗봇 서버 말고 어디에도 연결하지 않는다

의존성
------
표준 라이브러리만 쓴다. (urllib, subprocess, threading)
pip install 이 필요 없고, PyInstaller 로 한 파일로 묶을 수 있다.

    pyinstaller --onefile --name relay relay.py
"""

import base64
import http.client
import json
import os
import re
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

VERSION = "0.2.0"

APP_DIR = os.path.join(
    os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "claude-relay")
CONFIG_PATH = os.path.join(APP_DIR, "relay.json")

# 챗봇 서버가 긴 대기(기본 25초)를 쓰므로 읽기 제한은 그보다 넉넉해야 한다.
HTTP_TIMEOUT = 70
BEAT_SECONDS = 5
RESULT_FLUSH = 0.15

# 터미널 화면을 한 번에 올려 보낼 최대 크기
CHUNK_MAX = 16384


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
    # 이 PC 의 다른 사용자가 읽지 못하게 한다. (토큰 하나뿐이지만 그것도 비밀이다)
    _lock_down(CONFIG_PATH)


def _lock_down(path):
    if os.name != "nt":
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        return
    # 윈도우: 상속을 끊고 지금 사용자에게만 권한을 준다.
    user = os.environ.get("USERNAME") or ""
    if not user:
        return
    try:
        subprocess.run(["icacls", path, "/inheritance:r",
                        "/grant:r", "%s:F" % user],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       check=False)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
class Server(object):
    def __init__(self, url, key=None, insecure=False):
        self.url = url.rstrip("/")
        self.key = key
        self.ctx = None
        if insecure:
            # 사내 사설 인증서를 쓰는 경우에만. 기본은 검사한다.
            self.ctx = ssl.create_default_context()
            self.ctx.check_hostname = False
            self.ctx.verify_mode = ssl.CERT_NONE

    def post(self, path, payload, timeout=HTTP_TIMEOUT):
        body = json.dumps(payload or {}, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(self.url + path, data=body, method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("User-Agent", "claude-relay/%s" % VERSION)
        if self.key:
            req.add_header("X-Relay-Key", self.key)
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=self.ctx) as res:
                return json.loads(res.read().decode("utf-8") or "{}")
        except urllib.error.HTTPError as exc:
            try:
                data = json.loads(exc.read().decode("utf-8") or "{}")
            except ValueError:
                data = {}
            data["_status"] = exc.code
            return data


# ---------------------------------------------------------------------------
# 터널용 HTTP
#
# 클라이언트(claude-term)에도 같은 모양이 있다. 두 프로그램 모두 파일 하나로
# 묶여 따로 나가므로 서로 import 하지 않는다. 규약을 바꾸면 둘 다 고친다.
#
#   위로  : POST {"seq": n, "data": base64}. 연결을 재사용한다. 키 하나마다
#           TLS 를 새로 맺으면 그것만으로 수십 ms 가 붙는다.
#   아래로 : 쥐고 있는 GET. 한 줄짜리 프레임 (O / D <b64> / H / C <json>)
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
        """반환: (상태 코드, dict). 연결이 끊겼으면 한 번 새로 맺어 다시 보낸다."""
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
        """
        아래 스트림. 반환: (상태 코드, 프레임 iterator 또는 오류 dict)

        timeout 은 심박(서버 기본 15초)보다 넉넉해야 한다. 그보다 오래 아무
        줄도 안 오면 연결이 죽은 것이다.
        """
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
    """
    조각 하나를 올린다. 실패하면 **같은 순번으로** 다시 보낸다(서버는 같은
    순번을 두 번 받으면 두 번째를 버린다). 반환: 계속해도 되는가.
    """
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
        if status == 503:              # 반대편이 못 따라온다. 같은 순번으로 다시
            time.sleep(0.2)
            continue
        return False                   # 409(순서) · 410(닫힘) · 403 · 401
    return False


# ---------------------------------------------------------------------------
# ssh 실행
# ---------------------------------------------------------------------------
def which(name):
    from shutil import which as _w
    return _w(name)


def here():
    """
    relay.exe (또는 relay.py) 가 있는 폴더.

    shutil.which 가 보는 "현재 폴더" 와 다르다. 그쪽은 프로세스의 작업
    디렉터리라서, 서비스로 등록하거나 바로가기로 띄우면 엉뚱한 곳을 가리킨다.
    """
    target = sys.executable if getattr(sys, "frozen", False) else __file__
    return os.path.dirname(os.path.abspath(target))


def beside(name):
    """relay.exe 옆에 둔 프로그램. 묶어서 나눠 줄 때 여기에 넣는다."""
    guess = os.path.join(here(), name)
    return guess if os.path.exists(guess) else None


def ssh_path():
    found = beside("ssh.exe") or which("ssh")
    if found:
        return found
    guess = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"),
                         "System32", "OpenSSH", "ssh.exe")
    return guess if os.path.exists(guess) else None


def plink_path():
    """
    plink.exe 를 찾는다. **relay.exe 옆을 가장 먼저** 본다.

    관리자가 relay.exe 와 plink.exe 를 한 폴더에 묶어 zip 으로 올려 두면,
    쓰는 사람은 풀기만 하면 된다. VDI 마다 PuTTY 를 설치하지 않아도 되고,
    어느 plink 를 쓰는지도 분명해진다.
    """
    found = beside("plink.exe") or which("plink")
    if found:
        return found
    for base in (r"C:\Program Files\PuTTY", r"C:\Program Files (x86)\PuTTY"):
        guess = os.path.join(base, "plink.exe")
        if os.path.exists(guess):
            return guess
    return None


def key_file(name):
    """키 이름을 이 PC 의 실제 경로로 바꾼다. 이름만 받고 내용은 받지 않는다."""
    if not name:
        return None
    if os.path.sep in name or "/" in name:
        return os.path.expanduser(name)
    return os.path.join(os.path.expanduser("~"), ".ssh", name)


SSH_COMMON = [
    "-o", "BatchMode=yes",
    "-o", "StrictHostKeyChecking=accept-new",
    "-o", "ConnectTimeout=8",
    "-o", "ServerAliveInterval=15",
    "-o", "NumberOfPasswordPrompts=0",
]


def build_command(auth, command=None, interactive=False):
    """
    ssh / plink 명령줄을 만든다. 반환: (argv, "ssh" 또는 "plink")

    비밀번호는 argv 에 넣는다. plink 가 그것밖에 받지 않는다. 같은 PC 에서
    다른 사용자가 프로세스 목록으로 볼 수 있다는 뜻이므로, 키 인증을
    권하는 이유가 여기에 하나 더 있다.
    """
    host = auth.get("host") or ""
    port = int(auth.get("port") or 22)
    user = auth.get("username") or ""
    target = "%s@%s" % (user, host)

    if auth.get("kind") == "password":
        exe = plink_path()
        if not exe:
            raise RuntimeError(
                "plink.exe 를 찾지 못했습니다. 윈도우의 ssh 는 비밀번호를 "
                "비대화형으로 넣지 못하므로 PuTTY 의 plink.exe 가 필요합니다. "
                "PuTTY 를 설치하거나 키 인증으로 바꾸세요.")
        argv = [exe, "-ssh", "-batch", "-P", str(port),
                "-pw", auth.get("password") or ""]
        if interactive:
            argv.append("-t")
        argv.append(target)
        if command:
            argv.append(command)
        return argv, "plink"

    exe = ssh_path()
    if not exe:
        raise RuntimeError("ssh.exe 를 찾지 못했습니다. Windows 기능에서 "
                           "OpenSSH 클라이언트를 설치하세요.")
    argv = [exe, "-p", str(port)] + list(SSH_COMMON)
    kf = key_file(auth.get("key_name"))
    if kf:
        if not os.path.exists(kf):
            raise RuntimeError("키 파일이 없습니다: %s" % kf)
        argv += ["-i", kf, "-o", "IdentitiesOnly=yes"]
    if interactive:
        argv += ["-tt"]
    argv.append(target)
    if command:
        argv.append(command)
    return argv, "ssh"


def run_once(auth, command, timeout):
    """명령 하나를 돌리고 끝낸다. 반환: dict(ok, exit_code, output, elapsed, error)"""
    started = time.time()
    try:
        argv, kind = build_command(auth, command=command)
    except RuntimeError as exc:
        return {"ok": False, "exit_code": None, "output": "",
                "elapsed": 0, "error": str(exc)}
    try:
        proc = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              stdin=subprocess.DEVNULL, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"ok": False, "exit_code": None, "output": "",
                "elapsed": round(time.time() - started, 2),
                "error": "%d초 안에 끝나지 않아 끊었습니다." % timeout}
    except OSError as exc:
        return {"ok": False, "exit_code": None, "output": "",
                "elapsed": round(time.time() - started, 2), "error": str(exc)}

    out = (proc.stdout or b"").decode("utf-8", "replace")
    elapsed = round(time.time() - started, 2)
    err = ""
    if proc.returncode != 0 and _looks_like_connect_error(out):
        err = _error_line(out)
    return {"ok": proc.returncode == 0, "exit_code": proc.returncode,
            "output": out, "elapsed": elapsed, "error": err}


_ERROR_MARKS = ("connection refused", "connection timed out", "no route to host",
                "permission denied", "host key", "could not resolve",
                "authentication fail", "access denied", "fatal:",
                "operation timed out", "network is unreachable", "too open",
                "bad permissions", "no such file")


def _first_line(text):
    for line in (text or "").splitlines():
        if line.strip():
            return line.strip()[:200]
    return ""


def _looks_like_connect_error(text):
    low = (text or "").lower()
    return any(mark in low for mark in _ERROR_MARKS)


def _error_line(text):
    """
    왜 실패했는지가 적힌 **그 줄**을 고른다.

    `ssh -v` 의 출력은 첫 줄이 버전 배너다. 그래서 "첫 줄" 을 쓰면
    `debug1: OpenSSH_10.0p2 ...` 가 실패 이유로 올라간다. 그것은 아무 설명도
    아니다. 사람이 고칠 수 있는 줄은 "Connection refused" 가 적힌 줄이다.
    """
    lines = [x.strip() for x in (text or "").replace("\r", "").split("\n") if x.strip()]
    # 1) debug 가 아닌 줄에서 먼저 찾는다 (ssh 가 사람에게 하는 말)
    for line in lines:
        low = line.lower()
        if low.startswith("debug"):
            continue
        if any(mark in low for mark in _ERROR_MARKS):
            return line[:200]
    # 2) 없으면 debug 줄에서라도 찾는다
    for line in lines:
        low = line.lower()
        if any(mark in low for mark in _ERROR_MARKS):
            return re.sub(r"^debug\d*:\s*", "", line)[:200]
    # 3) 그래도 없으면 debug 가 아닌 마지막 줄 (보통 마지막 말이 결론이다)
    plain = [x for x in lines if not x.lower().startswith("debug")]
    return (plain[-1] if plain else "")[:200]


def run_test(auth, timeout=20):
    """
    연결 테스트. 붙어서 한 줄 받고 끊는다.

    배너(상대 ssh 의 종류와 버전)는 -v 의 디버그 줄에서 꺼낸다. 사람이
    "맞는 서버에 붙었다" 를 확인할 수 있는 가장 짧은 정보다.
    """
    started = time.time()
    try:
        argv, kind = build_command(auth, command="echo __RELAY_OK__")
    except RuntimeError as exc:
        return {"ok": False, "message": str(exc), "banner": "", "elapsed": 0,
                "error": str(exc)}
    if kind == "ssh":
        argv.insert(1, "-v")
    try:
        proc = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              stdin=subprocess.DEVNULL, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"ok": False, "message": "%d초 안에 답이 없습니다." % timeout,
                "banner": "", "elapsed": round(time.time() - started, 2),
                "error": "시간 초과"}
    except OSError as exc:
        return {"ok": False, "message": str(exc), "banner": "",
                "elapsed": round(time.time() - started, 2), "error": str(exc)}

    out = (proc.stdout or b"").decode("utf-8", "replace")
    dbg = (proc.stderr or b"").decode("utf-8", "replace")
    elapsed = round(time.time() - started, 2)
    ok = "__RELAY_OK__" in out

    banner = ""
    for line in dbg.splitlines():
        if "remote software version" in line:
            banner = "SSH-2.0-" + line.split("remote software version", 1)[1].strip()
            break

    who = "%s@%s" % (auth.get("username"), auth.get("host"))
    if ok:
        bits = ["%s초" % elapsed]
        if banner:
            bits.append(banner)
        bits.append(who)
        if auth.get("kind") == "password":
            bits.append("비밀번호 받아들여짐")
        elif auth.get("key_name"):
            bits.append("키 %s 받아들여짐" % auth["key_name"])
        return {"ok": True, "message": "연결 확인 · " + " · ".join(bits),
                "banner": banner, "elapsed": elapsed, "error": ""}

    why = _error_line(dbg) or _error_line(out) or "붙지 못했습니다"
    if "host key" in (dbg + out).lower() and auth.get("kind") == "password":
        why += " (VDI 에서 plink -ssh %s 를 한 번 실행해 호스트 키를 받아 두세요)" % who
    return {"ok": False, "message": "연결 실패 · " + why, "banner": banner,
            "elapsed": elapsed, "error": why}


# ---------------------------------------------------------------------------
# 터미널 세션
# ---------------------------------------------------------------------------
class Terminal(object):
    """ssh/plink 프로세스 한 개. 읽기 전용 스레드가 화면을 퍼 올린다."""

    def __init__(self, term_id, auth, sink, cols=120, rows=30):
        self.id = term_id
        self.sink = sink                  # 결과 큐
        self.proc = None
        self.alive = False
        self.reader = None
        self.cols = cols
        self.rows = rows
        argv, self.kind = build_command(auth, interactive=True)
        self.argv = argv

    def start(self):
        creation = 0
        if os.name == "nt":
            creation = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        self.proc = subprocess.Popen(
            self.argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, bufsize=0, creationflags=creation)
        self.alive = True
        self.reader = threading.Thread(target=self._pump, name="term-%s" % self.id[:8],
                                       daemon=True)
        self.reader.start()

    def _pump(self):
        """
        화면을 퍼 올린다.

        os.read 를 쓴다. 한 글자라도 오면 바로 돌아오고, 더 와 있으면 함께
        가져온다. 줄 단위로 읽으면(readline) 프롬프트처럼 줄바꿈 없이 끝나는
        출력이 엔터를 칠 때까지 화면에 나타나지 않는다.
        """
        fd = self.proc.stdout.fileno()
        try:
            while True:
                data = os.read(fd, CHUNK_MAX)
                if not data:
                    break
                self.sink.put(("term", {
                    "term_id": self.id,
                    "data": data.decode("utf-8", "replace"),
                }))
        except (OSError, ValueError):
            pass
        finally:
            self.alive = False
            code = self.proc.poll()
            self.sink.put(("term", {
                "term_id": self.id, "data": "",
                "closed": True,
                "reason": "서버 쪽에서 연결이 끊어졌습니다 (종료 %s)" % code,
            }))

    def write(self, text):
        if not self.alive or self.proc is None:
            return
        try:
            self.proc.stdin.write(text.encode("utf-8", "replace"))
            self.proc.stdin.flush()
        except (OSError, ValueError):
            self.alive = False

    def close(self):
        self.alive = False
        if self.proc is None:
            return
        try:
            self.proc.stdin.close()
        except (OSError, ValueError):
            pass
        try:
            self.proc.terminate()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# PuTTY 터널
# ---------------------------------------------------------------------------
class Tunnel(object):
    """
    대상 서버로 가는 소켓 하나와 챗봇 서버로 가는 HTTP 두 줄.

    주소는 서버가 보낸 것(관리자가 DB 에 적은 값)이다. 자격증명은 오지 않는다.
    ssh 도 plink 도 쓰지 않는다 — 키 파일 · 호스트 키 · 비밀번호가 여기에는 없다.
    """

    def __init__(self, agent, tunnel_id, host, port):
        self.agent = agent
        self.id = tunnel_id
        self.host = host
        self.port = int(port)
        self.sock = None
        self.stop = threading.Event()
        cfg = agent.cfg
        self.link = Link(cfg["url"], {"X-Relay-Key": cfg.get("agent_key") or "",
                                      "User-Agent": "claude-relay/%s" % VERSION},
                         insecure=bool(cfg.get("insecure")))
        self.down_link = Link(cfg["url"], self.link.headers,
                              insecure=bool(cfg.get("insecure")))

    def start(self):
        threading.Thread(target=self._run, name="tunnel-%s" % self.id[:8],
                         daemon=True).start()

    def _path(self, tail):
        return "/api/relay/tunnel/%s/%s" % (self.id, tail)

    def _run(self):
        try:
            self.sock = socket.create_connection((self.host, self.port), timeout=8)
        except OSError as exc:
            log("터널 %s : %s:%s 에 닿지 못했습니다 (%s)"
                % (self.id[:8], self.host, self.port, exc))
            self._report(False, str(exc)[:160])
            self.agent.tunnels.pop(self.id, None)
            return
        self.sock.settimeout(None)
        try:
            # 키 하나가 바로 나가야 한다. Nagle 이 모아 두면 PuTTY 가 굼떠 보인다.
            self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass
        self._report(True, "")
        log("터널 %s : %s:%s 열림" % (self.id[:8], self.host, self.port))
        threading.Thread(target=self._up, name="tunnel-up-%s" % self.id[:8],
                         daemon=True).start()
        try:
            self._down()
        finally:
            self.close()

    def _report(self, ok, error):
        try:
            self.link.post(self._path("opened"), {"ok": ok, "error": error})
        except (OSError, http.client.HTTPException) as exc:
            log("터널 %s : 열림을 알리지 못했습니다 (%s)" % (self.id[:8], exc))

    def _down(self):
        """챗봇 서버 → 대상. 프레임을 받아 소켓에 쓴다."""
        try:
            status, frames = self.down_link.stream(self._path("down"))
        except (OSError, http.client.HTTPException) as exc:
            log("터널 %s : 아래 스트림을 열지 못했습니다 (%s)" % (self.id[:8], exc))
            return
        if status != 200:
            log("터널 %s : 아래 스트림 거절 (%s)" % (self.id[:8], status))
            return
        try:
            for kind, value in frames:
                if self.stop.is_set():
                    return
                if kind == "D":
                    self.sock.sendall(value)
                elif kind == "C":
                    log("터널 %s : 닫힘 (%s)" % (self.id[:8], value))
                    return
        except (OSError, http.client.HTTPException, ValueError) as exc:
            log("터널 %s : 아래 스트림이 끊겼습니다 (%s)" % (self.id[:8], exc))

    def _up(self):
        """대상 → 챗봇 서버. 소켓에서 읽어 순번을 붙여 올린다."""
        seq = [0]
        why = "대상 서버가 연결을 끊었습니다"
        while not self.stop.is_set():
            try:
                data = self.sock.recv(32768)
            except OSError:
                data = b""
            if not data:
                break
            if not send_chunks(self.link, self._path("up"), seq, data, self.stop):
                why = ""
                break
        if why and not self.stop.is_set():
            try:
                self.link.post(self._path("close"), {"reason": why})
            except (OSError, http.client.HTTPException):
                pass
        self.close()

    def close(self, why=None):
        """
        닫는다. why 를 주면 챗봇 서버에 이유를 알린다(중계가 스스로 닫는 경우).
        서버가 먼저 닫은 경우에는 why 없이 부른다 — 서버가 이미 안다.
        """
        if self.stop.is_set():
            return
        self.stop.set()
        if why:
            try:
                self.link.post(self._path("close"), {"reason": why}, timeout=5)
            except (OSError, http.client.HTTPException):
                pass
        self.down_link.close()
        if self.sock is not None:
            try:
                self.sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                self.sock.close()
            except OSError:
                pass
        self.link.close()
        self.agent.tunnels.pop(self.id, None)


# ---------------------------------------------------------------------------
# 본 루프
# ---------------------------------------------------------------------------
class Agent(object):
    def __init__(self, cfg):
        self.cfg = cfg
        self.server = Server(cfg["url"], cfg.get("agent_key"),
                             insecure=bool(cfg.get("insecure")))
        self.out = queue.Queue()
        self.terms = {}
        self.tunnels = {}
        self.stop = threading.Event()
        self.poll_seconds = 25

    # --- 올려 보내기 ------------------------------------------------
    def _sender(self):
        while not self.stop.is_set():
            items = []
            try:
                items.append(self.out.get(timeout=0.5))
            except queue.Empty:
                continue
            time.sleep(RESULT_FLUSH)        # 조금 모아서 한 번에 보낸다
            while True:
                try:
                    items.append(self.out.get_nowait())
                except queue.Empty:
                    break
            payload = {"jobs": [], "term": []}
            for kind, item in items:
                payload["jobs" if kind == "job" else "term"].append(item)
            try:
                res = self.server.post("/api/relay/result", payload, timeout=30)
                if res.get("_status") == 401:
                    self._unauthorized()
            except (urllib.error.URLError, OSError, ValueError) as exc:
                log("결과를 올리지 못했습니다: %s" % exc)

    def _beater(self):
        while not self.stop.is_set():
            time.sleep(BEAT_SECONDS)
            alive = [t for t, obj in list(self.terms.items()) if obj.alive]
            if not alive:
                continue
            try:
                res = self.server.post("/api/relay/beat", {"terms": alive}, timeout=20)
                for term_id in (res.get("closed") or []):
                    self._close_term(term_id, "웹에서 닫았습니다")
            except (urllib.error.URLError, OSError, ValueError):
                pass

    def _unauthorized(self):
        log("등록이 해제되었습니다. relay register 로 다시 등록하세요.")
        self.stop.set()

    # --- 일 처리 ----------------------------------------------------
    def handle(self, job):
        kind = job.get("kind")
        jid = job.get("id")
        auth = job.get("auth") or {}
        payload = job.get("payload") or {}

        if kind == "test":
            draft = payload.get("draft")
            res = run_test(draft if draft else auth)
            self.out.put(("job", {"id": jid, "ok": res["ok"],
                                  "message": res["message"], "banner": res["banner"],
                                  "elapsed": res["elapsed"], "error": res["error"]}))
            return

        if kind == "run":
            timeout = int(payload.get("timeout") or 30)
            res = run_once(auth, payload.get("command") or "", timeout)
            self.out.put(("job", {"id": jid, "ok": res["ok"],
                                  "exit_code": res["exit_code"],
                                  "output": res["output"], "elapsed": res["elapsed"],
                                  "error": res["error"]}))
            return

        if kind == "term_open":
            term_id = job.get("term_id")
            try:
                term = Terminal(term_id, auth, self.out,
                                cols=int(payload.get("cols") or 120),
                                rows=int(payload.get("rows") or 30))
                term.start()
            except (RuntimeError, OSError) as exc:
                self.out.put(("job", {"id": jid, "ok": False, "error": str(exc),
                                      "message": str(exc)}))
                return
            self.terms[term_id] = term
            self.out.put(("job", {"id": jid, "ok": True, "message": "열었습니다"}))
            return

        if kind == "term_close":
            self._close_term(job.get("term_id"), "웹에서 닫았습니다")
            self.out.put(("job", {"id": jid, "ok": True, "message": "닫았습니다"}))
            return

        self.out.put(("job", {"id": jid, "ok": False,
                              "error": "모르는 일 종류: %s" % kind}))

    def _close_term(self, term_id, why):
        term = self.terms.pop(term_id, None)
        if term is None:
            return
        term.close()
        self.out.put(("term", {"term_id": term_id, "data": "",
                               "closed": True, "reason": why}))

    def feed(self, items):
        for item in items:
            term = self.terms.get(item.get("term_id"))
            if term is None:
                continue
            term.write(item.get("data") or "")

    # --- 루프 -------------------------------------------------------
    def run(self):
        threading.Thread(target=self._sender, name="sender", daemon=True).start()
        threading.Thread(target=self._beater, name="beater", daemon=True).start()
        log("중계 시작 · 서버 %s" % self.cfg["url"])
        backoff = 1
        while not self.stop.is_set():
            try:
                res = self.server.post("/api/relay/poll", {})
            except (urllib.error.URLError, OSError, ValueError) as exc:
                log("챗봇 서버에 닿지 못했습니다: %s (%d초 뒤 다시)" % (exc, backoff))
                time.sleep(backoff)
                backoff = min(30, backoff * 2)
                continue
            if res.get("_status") == 401:
                self._unauthorized()
                break
            if not res.get("ok"):
                log("응답이 이상합니다: %s" % str(res)[:200])
                time.sleep(3)
                continue

            backoff = 1
            self.poll_seconds = res.get("poll_seconds") or self.poll_seconds
            self.feed(res.get("input") or [])
            for job in (res.get("jobs") or []):
                # 일 하나가 오래 걸려도 다음 폴링이 밀리지 않게 따로 돌린다.
                threading.Thread(target=self.handle, args=(job,),
                                 name="job-%s" % job.get("id"), daemon=True).start()

            # 열어야 할 터널. 주소는 서버가 DB 에서 꺼낸 값이다.
            for t in (res.get("tunnels") or []):
                tid = t.get("tunnel_id")
                if not tid or tid in self.tunnels:
                    continue
                tunnel = Tunnel(self, tid, t.get("host"), t.get("port") or 22)
                self.tunnels[tid] = tunnel
                tunnel.start()

            # 열려 있어야 할 터미널만 남긴다 (웹에서 닫힌 것 정리)
            keep = set(res.get("open_terms") or [])
            for term_id in list(self.terms):
                if term_id not in keep:
                    self._close_term(term_id, "웹에서 닫혔습니다")
            # 터널도 같다. 서버가 닫은 터널의 소켓을 남겨 두지 않는다.
            if "open_tunnels" in res:
                keep = set(res.get("open_tunnels") or [])
                for tid, tunnel in list(self.tunnels.items()):
                    if tid not in keep:
                        tunnel.close()

        for term_id in list(self.terms):
            self._close_term(term_id, "중계가 멈췄습니다")
        for tunnel in list(self.tunnels.values()):
            # 바이트가 중계를 지나가므로 중계가 멈추면 터널도 끊긴다. 이유를 남긴다.
            tunnel.close("VDI 중계가 멈췄습니다")
        log("중계 종료")


def log(msg):
    sys.stdout.write("[%s] %s\n" % (time.strftime("%H:%M:%S"), msg))
    sys.stdout.flush()


# ---------------------------------------------------------------------------
# 명령
# ---------------------------------------------------------------------------
def ask(prompt):
    sys.stdout.write(prompt)
    sys.stdout.flush()
    return (sys.stdin.readline() or "").strip()


def cmd_register(argv):
    print("중계 등록")
    print("-" * 52)
    print("관리자 화면(중계 설정)에서 받은 두 줄을 적어 주세요.")
    print()
    url = argv[0] if argv else ask("서버 주소 : ")
    code = argv[1] if len(argv) > 1 else ask("등록 코드 : ")
    if not url or not code:
        print("두 줄 모두 필요합니다.")
        return 1
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    insecure = "--insecure" in argv
    server = Server(url, insecure=insecure)
    try:
        res = server.post("/api/relay/register", {
            "code": code,
            "name": os.environ.get("COMPUTERNAME") or os.uname().nodename,
            "version": VERSION,
            "os": "%s %s" % (os.name, sys.platform),
        }, timeout=30)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        print("서버에 닿지 못했습니다: %s" % exc)
        print("주소를 다시 확인하세요. 사내 사설 인증서라면 --insecure 를 붙이세요.")
        return 1

    if not res.get("ok"):
        print("등록하지 못했습니다: %s" % (res.get("error") or res))
        return 1

    save_config({"url": url, "agent_key": res["agent_key"],
                 "agent_id": res.get("agent_id"), "insecure": insecure,
                 "registered_at": time.strftime("%Y-%m-%d %H:%M:%S")})
    print()
    print("등록했습니다. 설정 파일 : %s" % CONFIG_PATH)
    print("이제 relay.exe (인자 없이) 를 실행해 두면 됩니다.")
    print("고칠 것이 생기면 웹만 고칩니다. 이 PC 는 다시 건드리지 않습니다.")
    return 0


def cmd_status():
    cfg = load_config()
    print("중계 상태")
    print("-" * 52)
    print("버전       : %s" % VERSION)
    print("설정 파일  : %s" % (CONFIG_PATH if cfg else "없음"))
    if not cfg:
        print()
        print("아직 등록하지 않았습니다. relay.exe register 로 등록하세요.")
        return 1
    print("서버 주소  : %s" % cfg["url"])
    print("이 프로그램 : %s" % here())
    print("ssh.exe    : %s" % (ssh_path() or "없음"))
    print("plink.exe  : %s" % (plink_path()
                               or "없음 (비밀번호 인증을 쓰면 필요. "
                                  "이 폴더에 plink.exe 를 넣어도 된다)"))
    server = Server(cfg["url"], cfg.get("agent_key"), insecure=bool(cfg.get("insecure")))
    try:
        res = server.post("/api/relay/beat", {"terms": []}, timeout=20)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        print("연결     : 닿지 못했습니다 (%s)" % exc)
        return 1
    if res.get("_status") == 401:
        print("연결       : 등록이 해제되었습니다. 다시 등록하세요.")
        return 1
    print("연결       : 좋습니다")
    print("열린 터미널: %d개" % len(res.get("open_terms") or []))
    return 0


def cmd_unregister():
    if not os.path.exists(CONFIG_PATH):
        print("설정 파일이 없습니다.")
        return 0
    os.remove(CONFIG_PATH)
    print("이 PC 의 설정을 지웠습니다. 챗봇 서버 쪽 등록은 관리자 화면에서 끊습니다.")
    return 0


def cmd_run():
    cfg = load_config()
    if not cfg:
        print("아직 등록하지 않았습니다. relay.exe register 로 등록하세요.")
        return 1
    agent = Agent(cfg)
    try:
        agent.run()
    except KeyboardInterrupt:
        agent.stop.set()
        print()
        log("멈춥니다")
    return 0


def main(argv):
    cmd = argv[1] if len(argv) > 1 else "run"
    rest = argv[2:]
    if cmd in ("register", "reg"):
        return cmd_register(rest)
    if cmd == "status":
        return cmd_status()
    if cmd == "unregister":
        return cmd_unregister()
    if cmd in ("run", "serve"):
        return cmd_run()
    if cmd in ("-h", "--help", "help"):
        print(__doc__)
        return 0
    print("모르는 명령입니다: %s" % cmd)
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
