#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tunnel_store
============

PuTTY 터널의 바닥. 라우트는 여기 있는 함수만 부른다. (Flask 를 import 하지 않는다)

무엇이 지나가는가
-----------------
    PuTTY ─ 127.0.0.1 ─ 클라이언트 ═ 챗봇 서버 ═ 중계(VDI) ─ 대상:22

SSH 는 PuTTY 와 sshd 사이에서 끝난다. 이 파일이 다루는 바이트는 **암호문**이다.
그래서 무엇을 쳤는지 남길 수 없고, 남기려고 하지도 않는다. 기록(tunnel_sessions)
에는 누가 · 언제 · 어느 서버 · 얼마나 · 몇 바이트만 남는다.

통로
----
챗봇 서버는 중계에도 클라이언트에도 먼저 걸지 못한다. 둘 다 **들어오는 방향**
으로만 붙는다. 한 터널에 네 줄이 있다.

    클라이언트 → 서버   POST  /api/client/tunnel/<id>/up     (조각마다)
    서버 → 클라이언트   GET   /api/client/tunnel/<id>/down   (쥐고 있는 응답)
    중계 → 서버         POST  /api/relay/tunnel/<id>/up      (조각마다)
    서버 → 중계         GET   /api/relay/tunnel/<id>/down    (쥐고 있는 응답)

조각에는 순번이 붙는다. 같은 순번이 두 번 오면(보내는 쪽이 응답을 못 받고 다시
보낸 경우) 두 번째는 버린다. 순번이 건너뛰면 그 자리에서 닫는다. SSH 바이트는
한 글자만 빠져도 그 뒤가 전부 깨지므로, 이어 붙이려 애쓰지 않는다.

아래로 흐르는 스트림은 한 줄짜리 프레임이다.

    O\\n                 터널이 열렸다 (클라이언트 쪽에만)
    D <base64>\\n        바이트
    H\\n                 심박. nginx 가 조용한 연결을 자르지 않게
    C <json 이유>\\n      닫혔다. 이 줄 뒤로는 아무것도 오지 않는다

왜 메모리인가
-------------
gunicorn 워커가 1개다(workers=1). 스레드끼리 같은 Hub 를 본다. 터널은 서버가
다시 뜨면 어차피 끊긴다. DB 에는 기록만 둔다.
"""

import base64
import collections
import hashlib
import json
import secrets
import threading
import time

import config
import settings_store
from db import ts

SIDES = ("client", "relay")

# 터널을 열어 달라는 요청이 중계에 닿지 않으면 이 시간 뒤에 닫는다.
OPEN_TIMEOUT_SECONDS = 30
# 한 번에 올릴 수 있는 조각 크기
MAX_CHUNK = 64 * 1024


def tunnel_idle_seconds(db):
    return max(60, min(3600, settings_store.get_int(db, "relay_tunnel_idle_seconds", 600)))


def _hash(value):
    return hashlib.sha256((value or "").encode("utf-8")).hexdigest()


def version_tuple(v):
    out = []
    for part in str(v or "").split("."):
        digits = "".join(ch for ch in part if ch.isdigit())
        out.append(int(digits) if digits else 0)
    while len(out) < 3:
        out.append(0)
    return tuple(out[:3])


def version_ok(v):
    return version_tuple(v) >= version_tuple(config.CLIENT_MIN_VERSION)


# ---------------------------------------------------------------------------
# 클라이언트 등록
#
# 중계 등록과 같은 모양이다. 웹에서 로그인한 사람이 자기 코드를 받고, 바깥 PC 의
# 클라이언트가 그 코드를 한 번 내고 키를 받는다. 그 키는 **그 사람**이다.
# ---------------------------------------------------------------------------
def new_client_code(db, user_id):
    """6자리 코드. 원문은 지금 한 번만 돌려준다. 그 사람이 전에 받은 코드만 죽인다."""
    code = "%06d" % secrets.randbelow(1000000)
    db.execute("UPDATE client_enroll_codes SET used_at = ?"
               " WHERE used_at IS NULL AND created_by = ?", (ts(), user_id))
    db.execute(
        "INSERT INTO client_enroll_codes (code_hash, expires_at, created_at, created_by)"
        " VALUES (?, datetime('now','localtime','+%d seconds'), ?, ?)"
        % config.RELAY_ENROLL_TTL_SECONDS, (_hash(code), ts(), user_id))
    return code


def active_client_code(db, user_id):
    return db.execute(
        "SELECT created_at, expires_at FROM client_enroll_codes"
        " WHERE used_at IS NULL AND created_by = ?"
        "   AND expires_at > datetime('now','localtime')"
        " ORDER BY created_at DESC LIMIT 1", (user_id,)).fetchone()


def consume_client_code(db, code):
    """코드를 쓴다. 반환: (code_hash, 주인 id) 또는 None. 한 줄 UPDATE 로 한 번만 통한다."""
    h = _hash((code or "").strip().replace(" ", ""))
    cur = db.execute(
        "UPDATE client_enroll_codes SET used_at = ?"
        " WHERE code_hash = ? AND used_at IS NULL"
        "   AND expires_at > datetime('now','localtime')", (ts(), h))
    if cur.rowcount != 1:
        return None
    row = db.execute("SELECT created_by FROM client_enroll_codes WHERE code_hash = ?",
                     (h,)).fetchone()
    return (h, row["created_by"]) if row else None


def register_client(db, code_hash, owner_id, pw_stamp, name, version, os_info, ip):
    """
    키를 만든다. 원문은 지금 한 번만 돌려준다.

    한 사람에 한 대다. 다시 등록하면 그 사람의 예전 클라이언트만 끊는다.
    (PC 를 바꿨을 때 예전 PC 의 키가 살아 있으면 안 된다)
    """
    key = secrets.token_urlsafe(32)
    now = ts()
    cur = db.execute(
        "INSERT INTO client_agents (key_hash, owner_id, pw_stamp, name, version,"
        " os_info, ip, registered_at, last_seen_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (_hash(key), owner_id, pw_stamp, (name or "")[:100], (version or "")[:40],
         (os_info or "")[:120], (ip or "")[:64], now, now))
    cid = cur.lastrowid
    db.execute("UPDATE client_enroll_codes SET client_id = ? WHERE code_hash = ?",
               (cid, code_hash))
    db.execute("UPDATE client_agents SET revoked_at = ?"
               " WHERE id != ? AND owner_id = ? AND revoked_at IS NULL",
               (now, cid, owner_id))
    return cid, key


def client_by_key(db, key):
    if not key:
        return None
    return db.execute(
        "SELECT * FROM client_agents WHERE key_hash = ? AND revoked_at IS NULL",
        (_hash(key),)).fetchone()


def touch_client(db, client_id, ip=None, version=None):
    sets, args = ["last_seen_at = ?"], [ts()]
    if ip is not None:
        sets.append("ip = ?")
        args.append((ip or "")[:64])
    if version:
        sets.append("version = ?")
        args.append(version[:40])
    args.append(client_id)
    db.execute("UPDATE client_agents SET %s WHERE id = ?" % ", ".join(sets), args)


def my_client(db, user_id):
    """그 사람의 살아 있는 등록. (붙어 있는지는 last_seen_at 으로 본다)"""
    return db.execute(
        "SELECT * FROM client_agents WHERE owner_id = ? AND revoked_at IS NULL"
        " ORDER BY registered_at DESC LIMIT 1", (user_id,)).fetchone()


def client_is_live(row, within_seconds=120):
    if row is None or not row["last_seen_at"]:
        return False
    try:
        seen = time.mktime(time.strptime(row["last_seen_at"], "%Y-%m-%d %H:%M:%S"))
    except (TypeError, ValueError):
        return False
    return time.time() - seen <= within_seconds


def revoke_client(db, client_id):
    db.execute("UPDATE client_agents SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL",
               (ts(), client_id))


# ---------------------------------------------------------------------------
# 터널 (메모리)
# ---------------------------------------------------------------------------
def _other(side):
    return "relay" if side == "client" else "client"


class Pipe(object):
    """
    터널 하나. 양쪽 방향의 바이트와 상태를 들고 있다.

    lock 하나에 조건 세 개를 건다. 바이트가 들어오면 **받을 쪽** 스트림만
    깨운다. 하나로 두면 키 하나 칠 때마다 반대편 스트림까지 깨어 빈 심박을
    보낸다.
    """

    def __init__(self, tunnel_id, server_id, user_id, agent_owner, client_id, target):
        self.id = tunnel_id
        self.server_id = server_id
        self.user_id = user_id
        self.agent_owner = agent_owner        # 이 터널을 열 수 있는 중계의 주인
        self.agent_id = None                  # 실제로 가져간 중계
        self.client_id = client_id
        self.target = target                  # {"host", "port"} — DB 에 적힌 값만
        self.lock = threading.Lock()
        self.ready = {s: threading.Condition(self.lock) for s in SIDES}
        self.space = threading.Condition(self.lock)
        self.queue = {s: collections.deque() for s in SIDES}   # 그 쪽으로 갈 바이트
        self.queued = {s: 0 for s in SIDES}
        self.seq_in = {s: 0 for s in SIDES}                    # 그 쪽에서 받은 마지막 순번
        self.attached = {s: False for s in SIDES}
        self.state = "opening"
        self.reason = ""
        self.failed = False
        self.dispatched = False               # 중계에 "열어라" 를 건넸는가
        self.bytes_up = 0                     # 클라이언트 → 대상
        self.bytes_down = 0                   # 대상 → 클라이언트
        self.created = time.time()
        self.last_payload = time.time()
        self.closed_at = None
        self.persisted = False

    # --- 상태 ---------------------------------------------------------
    def mark_open(self, agent_id=None):
        with self.lock:
            if self.state == "opening":
                self.state = "open"
                if agent_id is not None:
                    self.agent_id = agent_id
                self.last_payload = time.time()
            for c in self.ready.values():
                c.notify_all()

    def close(self, reason, failed=False):
        """닫는다. 이미 닫혔으면 먼저 적힌 이유를 그대로 둔다."""
        with self.lock:
            if self.state == "closed":
                return False
            self.failed = failed or self.state == "opening"
            self.state = "closed"
            self.reason = (reason or "")[:200]
            self.closed_at = time.time()
            for c in self.ready.values():
                c.notify_all()
            self.space.notify_all()
            return True

    def is_closed(self):
        with self.lock:
            return self.state == "closed"

    # --- 들어오는 바이트 ----------------------------------------------
    def put(self, side, seq, data, wait=10.0):
        """
        side 쪽에서 온 조각을 반대편으로 넘긴다.

        반환: "ok" | "dup"(이미 받은 순번) | "gap"(순번이 건너뜀)
              | "busy"(반대편이 못 따라옴) | "closed"
        """
        dest = _other(side)
        deadline = time.time() + wait
        with self.lock:
            if self.state == "closed":
                return "closed"
            if seq <= self.seq_in[side]:
                return "dup"
            if seq != self.seq_in[side] + 1:
                return "gap"
            while self.queued[dest] > config.TUNNEL_BUFFER_BYTES and self.state != "closed":
                left = deadline - time.time()
                if left <= 0:
                    return "busy"
                self.space.wait(left)
            if self.state == "closed":
                return "closed"
            self.seq_in[side] = seq
            if data:
                self.queue[dest].append(data)
                self.queued[dest] += len(data)
                if side == "client":
                    self.bytes_up += len(data)
                else:
                    self.bytes_down += len(data)
                self.last_payload = time.time()
                self.ready[dest].notify_all()
            return "ok"

    # --- 나가는 스트림 ------------------------------------------------
    def attach(self, side):
        """그 쪽의 아래 스트림은 하나만. 두 번째는 거절한다."""
        with self.lock:
            if self.attached[side]:
                return False
            self.attached[side] = True
            return True

    def frames(self, side, heartbeat=None):
        """
        side 쪽으로 흐르는 아래 스트림. attach() 가 True 일 때만 부른다.

        제너레이터가 끝나는 경우는 셋이다. 터널이 닫혔다(C 를 보내고 끝),
        받는 쪽이 끊었다(쓰기가 실패해 gunicorn 이 제너레이터를 닫는다),
        서버가 내려간다. 앞의 것이 아니면 터널을 닫는다 — 바이트가 빠진
        SSH 는 다시 이어 붙일 수 없다.
        """
        hb = heartbeat or config.TUNNEL_HEARTBEAT_SECONDS
        told_open = side != "client"
        finished = False
        try:
            while True:
                with self.lock:
                    if not (self.queue[side] or self.state == "closed"
                            or (not told_open and self.state == "open")):
                        self.ready[side].wait(hb)
                    chunks = list(self.queue[side])
                    self.queue[side].clear()
                    if chunks:
                        self.queued[side] = 0
                        self.space.notify_all()
                    state, reason = self.state, self.reason
                out = []
                if not told_open and state == "open":
                    out.append(b"O\n")
                    told_open = True
                for c in chunks:
                    out.append(b"D " + base64.b64encode(c) + b"\n")
                if state == "closed" and not chunks:
                    out.append(b"C " + json.dumps(reason, ensure_ascii=False)
                               .encode("utf-8") + b"\n")
                    finished = True
                    yield b"".join(out)
                    return
                yield b"".join(out) if out else b"H\n"
        finally:
            with self.lock:
                self.attached[side] = False
            if not finished:
                self.close("클라이언트와 연결이 끊어졌습니다" if side == "client"
                           else "VDI 중계와 연결이 끊어졌습니다")

    def snapshot(self):
        with self.lock:
            return {"tunnel_id": self.id, "server_id": self.server_id,
                    "state": self.state, "reason": self.reason,
                    "bytes_up": self.bytes_up, "bytes_down": self.bytes_down,
                    "opened_for": round(time.time() - self.created, 1),
                    "idle_for": round(time.time() - self.last_payload, 1)}


class Hub(object):
    def __init__(self):
        self._lock = threading.Lock()
        self._pipes = {}

    def create(self, *args):
        p = Pipe(*args)
        with self._lock:
            self._pipes[p.id] = p
        return p

    def get(self, tunnel_id):
        with self._lock:
            return self._pipes.get(tunnel_id)

    def drop(self, tunnel_id):
        with self._lock:
            return self._pipes.pop(tunnel_id, None)

    def all(self):
        with self._lock:
            return list(self._pipes.values())

    def live(self):
        return [p for p in self.all() if not p.is_closed()]

    def take_dispatch(self, owner_id):
        """
        중계가 열어야 할 터널. [{"tunnel_id", "host", "port"}]

        **그 중계의 주인 것만** 준다. 일 큐(relay_jobs)와 같은 규칙이다.
        한 번 건넨 것은 다시 건네지 않는다.
        """
        out = []
        for p in self.all():
            with p.lock:
                if p.dispatched or p.state != "opening" or p.agent_owner != owner_id:
                    continue
                p.dispatched = True
                out.append({"tunnel_id": p.id, "host": p.target["host"],
                            "port": p.target["port"]})
        return out

    def has_dispatch(self, owner_id):
        for p in self.all():
            if not p.dispatched and p.state == "opening" and p.agent_owner == owner_id:
                return True
        return False


HUB = Hub()


# ---------------------------------------------------------------------------
# 터널 (DB)
# ---------------------------------------------------------------------------
def open_tunnel_row(db, tunnel_id, server_id, user_id, client_id, target, client_ip):
    db.execute(
        "INSERT INTO tunnel_sessions (id, server_id, user_id, client_id, target, state,"
        " opened_at, client_ip) VALUES (?,?,?,?,?,'opening',?,?)",
        (tunnel_id, server_id, user_id, client_id, target, ts(), (client_ip or "")[:64]))


def tunnel_row(db, tunnel_id):
    return db.execute("SELECT * FROM tunnel_sessions WHERE id = ?", (tunnel_id,)).fetchone()


def set_open(db, tunnel_id, agent_id):
    db.execute("UPDATE tunnel_sessions SET state = 'open', agent_id = ?"
               " WHERE id = ? AND state = 'opening'", (agent_id, tunnel_id))


def persist_closed(db, pipe):
    """닫힌 터널을 기록에 적는다. 한 번만."""
    if pipe.persisted:
        return
    snap = pipe.snapshot()
    db.execute(
        "UPDATE tunnel_sessions SET state = ?, closed_at = COALESCE(closed_at, ?),"
        " bytes_up = ?, bytes_down = ?, agent_id = COALESCE(agent_id, ?),"
        " close_reason = CASE WHEN close_reason = '' THEN ? ELSE close_reason END"
        " WHERE id = ?",
        ("failed" if pipe.failed else "closed", ts(), snap["bytes_up"],
         snap["bytes_down"], pipe.agent_id, snap["reason"], pipe.id))
    pipe.persisted = True


def close_tunnel(db, pipe, reason, failed=False):
    pipe.close(reason, failed=failed)
    persist_closed(db, pipe)


def tunnel_max_per_user(db):
    """한 사람이 동시에 열 수 있는 PuTTY 탭(터널) 수. 웹 콘솔과 따로 센다."""
    return max(1, min(8, settings_store.get_int(db, "relay_tunnel_max_per_user", 4)))


def open_tunnel_count(db, user_id):
    return db.execute(
        "SELECT COUNT(*) AS c FROM tunnel_sessions"
        " WHERE user_id = ? AND state IN ('opening','open')", (user_id,)).fetchone()["c"]


def tunnels_for_user(db, user_id, limit=10):
    return db.execute(
        "SELECT t.*, s.name AS server_name FROM tunnel_sessions t"
        " LEFT JOIN ssh_servers s ON s.id = t.server_id"
        " WHERE t.user_id = ? ORDER BY t.opened_at DESC LIMIT ?",
        (user_id, limit)).fetchall()


def close_user_tunnels(db, user_id, why):
    """그 사람의 터널을 그 자리에서 닫는다. 허용을 떼거나 등록을 해제할 때 쓴다."""
    n = 0
    for p in HUB.all():
        if p.user_id == user_id and not p.is_closed():
            close_tunnel(db, p, why)
            n += 1
    # 메모리에 없는데 DB 에 열려 있는 줄 (지난 프로세스가 남긴 것)
    db.execute(
        "UPDATE tunnel_sessions SET state = 'closed', closed_at = ?, close_reason = ?"
        " WHERE user_id = ? AND state IN ('opening','open')", (ts(), why[:200], user_id))
    return n


def close_server_tunnels(db, server_id, why):
    n = 0
    for p in HUB.all():
        if p.server_id == server_id and not p.is_closed():
            close_tunnel(db, p, why)
            n += 1
    return n


def capacity_left(db):
    """
    터널 하나를 더 열 수 있는가.

    터널 하나는 아래 스트림 두 개가 스레드를 하나씩 쥔다. 중계의 롱폴도 하나씩
    쥔다. 남은 스레드가 채팅 몫(TUNNEL_RESERVED_THREADS) 밑으로 내려가면 거절한다.
    **터널 때문에 채팅이 멈추면 안 된다.**
    """
    relays = db.execute(
        "SELECT COUNT(*) AS c FROM relay_agents WHERE revoked_at IS NULL"
        "   AND last_seen_at >= datetime('now','localtime','-90 seconds')").fetchone()["c"]
    held = 2 * len(HUB.live()) + relays
    return held + 2 <= config.GUNICORN_THREADS - config.TUNNEL_RESERVED_THREADS


def close_orphan_tunnels(db):
    """기동할 때 한 번. 메모리가 비었으니 DB 에 열려 있는 줄은 살아 있을 수 없다."""
    cur = db.execute(
        "UPDATE tunnel_sessions SET state = 'closed', closed_at = ?,"
        " close_reason = '챗봇 서버가 다시 시작되어 닫혔습니다'"
        " WHERE state IN ('opening','open')", (ts(),))
    if cur.rowcount:
        db.commit()
    return cur.rowcount


def housekeep(db, still_allowed):
    """
    10초마다. relay_store.housekeep 과 같은 스레드에서 돈다.

    still_allowed(db, pipe) -> 이유 문자열 또는 "" : 권한을 다시 본다.
    허용을 떼는 화면은 그 자리에서 닫지만, 계정을 정지하거나 서버를 끄는
    다른 길도 있다. 그 모든 길을 여기서 한 번 더 받는다.
    """
    steps = []
    idle = tunnel_idle_seconds(db)
    now = time.time()
    for p in HUB.all():
        if p.is_closed():
            persist_closed(db, p)
            if p.closed_at and now - p.closed_at > 60:
                HUB.drop(p.id)
            continue
        if p.state == "opening" and now - p.created > OPEN_TIMEOUT_SECONDS:
            close_tunnel(db, p, "VDI 중계가 %d초 안에 대상 서버에 붙지 못했습니다"
                         % OPEN_TIMEOUT_SECONDS, failed=True)
            steps.append("터널 열기 시간 초과 %s" % p.id[:8])
            continue
        if p.state == "open" and now - p.last_payload > idle:
            close_tunnel(db, p, "%d분 동안 아무것도 지나가지 않아 닫았습니다"
                         % max(1, idle // 60))
            steps.append("조용한 터널 닫기 %s" % p.id[:8])
            continue
        why = still_allowed(db, p)
        if why:
            close_tunnel(db, p, why)
            steps.append("허용이 사라진 터널 닫기 %s" % p.id[:8])
    db.commit()
    return steps
