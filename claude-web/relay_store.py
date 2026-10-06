#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
relay_store
===========

SSH 중계의 바닥. 라우트는 여기 있는 함수만 부른다. (Flask 를 import 하지 않는다)

무엇이 어디에 있는가
--------------------
    DB (relay_jobs)   : 중계가 받아 갈 일 중 **기록에 남아야 하는 것**
                        test / run / term_open / term_close
    메모리 (hub)      : 터미널의 화면 내용과 사람이 친 키
                        워커가 1개(gunicorn workers=1)라서 스레드끼리 같은
                        버퍼를 본다. 그래서 외부 저장소가 필요 없다.

터미널 입력을 DB 에 넣지 않는 이유
----------------------------------
sudo 가 비밀번호를 물으면 사람은 그 자리에 비밀번호를 친다. 그 키를
relay_jobs.payload 에 넣으면 비밀번호가 DB 에 남는다. 그래서 터미널 입력은
어느 표에도 들어가지 않고 메모리만 거쳐 중계로 간다.

기록(term_inputs)에는 **엔터로 끝난 한 줄**만 남기고, 직전 출력이 비밀번호를
묻고 있었으면 내용을 적지 않고 가린다. (ssh_policy.looks_like_password_prompt)

중계 인증
---------
등록 코드(6자리, 한 번 쓰면 죽음)로 한 번 바꿔서 중계 토큰을 받는다. 토큰
원문은 저장하지 않고 sha256 만 둔다. 이후 모든 요청은 X-Relay-Key 헤더로
인증한다. 쿠키를 쓰지 않으므로 브라우저가 흉내낼 수 없고 CSRF 대상도 아니다.
"""

import collections
import hashlib
import json
import os
import secrets
import threading
import time

import config
import settings_store
import ssh_policy
from db import row_to_dict, ts

# ---------------------------------------------------------------------------
# 설정 읽기
# ---------------------------------------------------------------------------
def poll_seconds(db):
    return max(5, min(60, settings_store.get_int(db, "relay_poll_seconds", 25)))


def run_timeout(db):
    return max(5, min(120, settings_store.get_int(db, "relay_run_timeout", 30)))


def approval_seconds(db):
    return max(30, min(600, settings_store.get_int(db, "relay_approval_seconds", 120)))


def term_max_per_user(db):
    return max(1, min(4, settings_store.get_int(db, "relay_term_max_per_user", 2)))


def term_idle_seconds(db):
    return max(30, min(1800, settings_store.get_int(db, "relay_term_idle_seconds", 180)))


def chat_max_commands(db):
    return max(1, min(5, settings_store.get_int(db, "relay_chat_max_commands", 3)))


def queue_keep_days(db):
    return max(7, min(365, settings_store.get_int(db, "relay_queue_keep_days", 90)))


# ---------------------------------------------------------------------------
# 서버
# ---------------------------------------------------------------------------
AUTH_KEY = "key"
AUTH_PASSWORD = "password"
AUTH_KINDS = (AUTH_KEY, AUTH_PASSWORD)

AUTH_LABELS = {AUTH_KEY: "키", AUTH_PASSWORD: "비밀번호"}


def get_server(db, server_id):
    return db.execute("SELECT * FROM ssh_servers WHERE id = ?", (server_id,)).fetchone()


def get_server_by_name(db, name):
    return db.execute("SELECT * FROM ssh_servers WHERE name = ?", (name,)).fetchone()


def server_payload(row, with_admin=False):
    """
    화면으로 내보낼 서버 하나.

    secret_enc 는 어떤 경우에도 나가지 않는다. 비밀번호를 쓰는 서버인지
    (auth_kind) 와 값이 들어 있는지(has_secret) 까지만 알려 준다.
    """
    d = row_to_dict(row)
    raw = d.pop("secret_enc", "") or ""
    d["has_secret"] = bool(raw)
    d["secret_preview"] = settings_store.mask(settings_store.decrypt_secret(raw)) if raw else ""
    d["auth_label"] = AUTH_LABELS.get(d.get("auth_kind"), d.get("auth_kind"))
    d["is_enabled"] = bool(d.get("is_enabled"))
    if d.get("last_check_ok") is not None:
        d["last_check_ok"] = bool(d["last_check_ok"])
    d["address"] = "%s@%s:%s" % (d["username"], d["host"], d["port"])
    if not with_admin:
        d.pop("created_by", None)
    return d


def server_auth_for_relay(db, row):
    """
    중계에 실려 나갈 접속 정보. **이 함수의 결과는 로그에 찍지 않는다.**

    비밀번호는 이 순간에만 복호화된다. DB 에는 암호문만 있고, 중계는 받아서
    메모리에만 들고 쓰고 버린다. (디스크에 쓰지 않는다)
    """
    kind = row["auth_kind"]
    out = {"kind": kind, "username": row["username"], "host": row["host"],
           "port": int(row["port"] or 22)}
    if kind == AUTH_PASSWORD:
        out["password"] = settings_store.decrypt_secret(row["secret_enc"] or "")
    else:
        out["key_name"] = row["key_name"] or ""
    return out


def touch_server_check(db, server_id, ok, message):
    db.execute(
        "UPDATE ssh_servers SET last_check_at = ?, last_check_ok = ?, last_check_msg = ?"
        " WHERE id = ?", (ts(), 1 if ok else 0, (message or "")[:300], server_id))


# ---------------------------------------------------------------------------
# 중계 등록 / 인증
# ---------------------------------------------------------------------------
def _hash(value):
    return hashlib.sha256((value or "").encode("utf-8")).hexdigest()


def new_enroll_code(db, user_id):
    """
    6자리 등록 코드를 만든다. 원문은 지금 한 번만 돌려주고 해시만 남긴다.

    코드는 **그 사람 것**이다. 그 코드로 등록한 중계는 그 사람의 중계가 된다.
    새로 만들면 그 사람이 전에 받은 코드만 죽는다. 남의 코드는 건드리지 않는다.
    """
    code = "%06d" % secrets.randbelow(1000000)
    db.execute(
        "UPDATE relay_enroll_codes SET used_at = ?"
        " WHERE used_at IS NULL AND created_by = ?", (ts(), user_id))
    db.execute(
        "INSERT INTO relay_enroll_codes (code_hash, expires_at, created_at, created_by)"
        " VALUES (?, datetime('now','localtime','+%d seconds'), ?, ?)"
        % config.RELAY_ENROLL_TTL_SECONDS,
        (_hash(code), ts(), user_id))
    return code


def active_enroll_code(db, user_id=None):
    """아직 쓰지 않고 살아 있는 코드의 메타데이터. 원문은 어디에도 없다."""
    sql = ("SELECT created_at, expires_at, created_by FROM relay_enroll_codes"
           " WHERE used_at IS NULL AND expires_at > datetime('now','localtime')")
    args = []
    if user_id is not None:
        sql += " AND created_by = ?"
        args.append(user_id)
    return db.execute(sql + " ORDER BY created_at DESC LIMIT 1", args).fetchone()


def enroll_code_owner(db, code_hash):
    """그 코드를 받은 사람. 등록한 중계의 주인이 된다."""
    row = db.execute("SELECT created_by FROM relay_enroll_codes WHERE code_hash = ?",
                     (code_hash,)).fetchone()
    return row["created_by"] if row else None


def consume_enroll_code(db, code):
    """
    코드를 쓴다. 성공하면 code_hash, 실패하면 None.
    같은 코드로 두 대가 붙지 못하게 UPDATE 한 줄로 처리한다.
    """
    h = _hash((code or "").strip().replace(" ", ""))
    cur = db.execute(
        "UPDATE relay_enroll_codes SET used_at = ?"
        " WHERE code_hash = ? AND used_at IS NULL"
        "   AND expires_at > datetime('now','localtime')", (ts(), h))
    return h if cur.rowcount == 1 else None


def register_agent(db, code_hash, name, version, os_info, ip, scheme):
    """
    중계 토큰을 만든다. 원문은 지금 한 번만 돌려준다.

    중계의 주인은 그 등록 코드를 받은 사람이다. **한 사람에 한 대**이므로 그
    사람이 다시 등록하면 그 사람의 예전 중계만 끊는다. (VDI 를 다시 깔았을 때
    유령 중계가 일을 받아 가는 것을 막는다) 남의 중계는 건드리지 않는다.
    """
    owner_id = enroll_code_owner(db, code_hash)
    key = secrets.token_urlsafe(32)
    cur = db.execute(
        "INSERT INTO relay_agents (key_hash, owner_id, name, version, os_info, ip,"
        " scheme, registered_at, last_seen_at, created_by)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)",
        (_hash(key), owner_id, (name or "")[:100], (version or "")[:40],
         (os_info or "")[:120], (ip or "")[:64], (scheme or "")[:10], ts(), ts(),
         owner_id))
    agent_id = cur.lastrowid
    db.execute("UPDATE relay_enroll_codes SET agent_id = ? WHERE code_hash = ?",
               (agent_id, code_hash))
    if owner_id is not None:
        db.execute("UPDATE relay_agents SET revoked_at = ?"
                   " WHERE id != ? AND owner_id = ? AND revoked_at IS NULL",
                   (ts(), agent_id, owner_id))
    return agent_id, key


def agent_by_key(db, key):
    if not key:
        return None
    return db.execute(
        "SELECT * FROM relay_agents WHERE key_hash = ? AND revoked_at IS NULL",
        (_hash(key),)).fetchone()


def agent_revoked(db, agent_id):
    row = db.execute("SELECT revoked_at FROM relay_agents WHERE id = ?",
                     (agent_id,)).fetchone()
    return row is None or row["revoked_at"] is not None


def touch_agent(db, agent_id, ip=None, scheme=None):
    if ip is None and scheme is None:
        db.execute("UPDATE relay_agents SET last_seen_at = ? WHERE id = ?",
                   (ts(), agent_id))
    else:
        db.execute("UPDATE relay_agents SET last_seen_at = ?, ip = ?, scheme = ?"
                   " WHERE id = ?", (ts(), (ip or "")[:64], (scheme or "")[:10], agent_id))


def live_agent(db, user_id, within_seconds=90):
    """
    **그 사람의** 중계. 붙어 있지 않으면 None.

    남의 중계가 붙어 있어도 소용이 없다. 일은 그 사람의 중계로만 나간다.
    """
    if not user_id:
        return None
    return db.execute(
        "SELECT * FROM relay_agents WHERE revoked_at IS NULL AND owner_id = ?"
        "   AND last_seen_at >= datetime('now','localtime','-%d seconds')"
        " ORDER BY last_seen_at DESC LIMIT 1" % int(within_seconds),
        (user_id,)).fetchone()


def live_agents(db, within_seconds=90):
    """지금 붙어 있는 중계 전부. 관리자 화면이 쓴다."""
    return db.execute(
        "SELECT a.*, u.username, u.display_name FROM relay_agents a"
        " LEFT JOIN users u ON u.id = a.owner_id"
        " WHERE a.revoked_at IS NULL"
        "   AND a.last_seen_at >= datetime('now','localtime','-%d seconds')"
        " ORDER BY a.last_seen_at DESC" % int(within_seconds)).fetchall()


def all_agents(db, limit=50):
    """끊긴 것까지 포함한 목록. 관리자 화면이 쓴다."""
    return db.execute(
        "SELECT a.*, u.username, u.display_name FROM relay_agents a"
        " LEFT JOIN users u ON u.id = a.owner_id"
        " ORDER BY a.revoked_at IS NOT NULL, a.last_seen_at DESC LIMIT ?",
        (limit,)).fetchall()


def revoke_agent(db, agent_id):
    db.execute("UPDATE relay_agents SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL",
               (ts(), agent_id))


# ---------------------------------------------------------------------------
# 일 큐
# ---------------------------------------------------------------------------
KIND_TEST = "test"
KIND_RUN = "run"
KIND_TERM_OPEN = "term_open"
KIND_TERM_CLOSE = "term_close"

_WAKE = threading.Condition()

# ---------------------------------------------------------------------------
# 큐를 거치지 않고 메모리로만 다니는 두 가지
#
#   1. 초안 테스트의 비밀번호
#      저장 전 「연결 테스트」는 아직 DB 에 없는 서버를 두드린다. 그 비밀번호를
#      relay_jobs.payload 에 적으면 "어느 표에도 비밀을 넣지 않는다" 가 깨진다.
#   2. 명령의 출력
#      출력은 그 대화에만 남아야 한다(messages). 큐에 적어 두면 90일 동안
#      남아서, private 대화의 내용을 대화 밖에서 읽을 수 있게 된다.
#
# 워커가 1개(gunicorn workers=1)라서 기다리는 요청 스레드와 결과를 올리는
# 스레드가 같은 메모리를 본다. 그래서 이 둘은 DB 를 거칠 필요가 없다.
# 못 받아 간 것은 housekeep 이 치운다.
# ---------------------------------------------------------------------------
_SIDE = {"draft": {}, "output": {}}
_SIDE_GUARD = threading.Lock()
_SIDE_TTL = 300.0


def put_side(kind, job_id, value):
    with _SIDE_GUARD:
        _SIDE[kind][int(job_id)] = (value, time.time())


def take_side(kind, job_id):
    """한 번만 꺼낸다. 꺼내면 메모리에서 사라진다."""
    with _SIDE_GUARD:
        item = _SIDE[kind].pop(int(job_id), None)
    return item[0] if item else None


def sweep_side():
    dropped = 0
    cutoff = time.time() - _SIDE_TTL
    with _SIDE_GUARD:
        for kind in _SIDE:
            for job_id in [k for k, v in _SIDE[kind].items() if v[1] < cutoff]:
                del _SIDE[kind][job_id]
                dropped += 1
    return dropped


def wake():
    """기다리고 있는 중계 poll 을 깨운다."""
    with _WAKE:
        _WAKE.notify_all()


def wait_for_work(timeout):
    with _WAKE:
        _WAKE.wait(timeout)


def enqueue(db, kind, owner_id, server_id=None, term_id="", payload=None,
            requested_by=None):
    """
    할 일을 큐에 넣는다.

    owner_id 는 "누구의 중계가 이 일을 가져갈 수 있는가" 다. 반드시 넣는다.
    비워 두면 아무 중계도 가져가지 못한다. 그 편이 남의 중계가 가져가는 것보다
    낫다. 모르는 PC 에서 도는 명령보다 돌지 않는 명령이 낫기 때문이다.
    """
    cur = db.execute(
        "INSERT INTO relay_jobs (kind, owner_id, server_id, term_id, payload, state,"
        " requested_by, created_at) VALUES (?,?,?,?,?,'queued',?,?)",
        (kind, owner_id, server_id, term_id or "",
         json.dumps(payload or {}, ensure_ascii=False),
         requested_by if requested_by is not None else owner_id, ts()))
    return cur.lastrowid


def take_jobs(db, agent_id, owner_id, limit=8):
    """
    큐에서 할 일을 꺼낸다. **주인이 같은 일만** 꺼낸다.

    꺼낸 순간 taken 으로 바꿔 두 번 나가지 않게 한다. 워커가 1개라도 스레드는
    여러 개다. UPDATE ... WHERE state='queued' 의 rowcount 로 임자를 정한다.
    (SELECT 뒤 UPDATE 사이에 끼어들 수 없다)
    """
    rows = db.execute(
        "SELECT * FROM relay_jobs WHERE state = 'queued' AND owner_id = ?"
        " ORDER BY id LIMIT ?", (owner_id, limit)).fetchall()
    taken = []
    for r in rows:
        cur = db.execute(
            "UPDATE relay_jobs SET state = 'taken', agent_id = ?, taken_at = ?"
            " WHERE id = ? AND state = 'queued'", (agent_id, ts(), r["id"]))
        if cur.rowcount == 1:
            taken.append(r)
    # 가져간 것이 없어도 **반드시** 커밋한다.
    #
    # UPDATE 가 0행을 고쳤어도(남이 먼저 가져갔다) 쓰기 트랜잭션은 이미 열렸다.
    # 그대로 두고 긴 대기(25초)에 들어가면 그 25초 동안 이 연결이 쓰기 락을
    # 들고 있어서 다른 모든 요청의 쓰기가 막힌다. busy_timeout(15초)을 넘기면
    # "database is locked" 로 500 이 난다. 실제로 그렇게 났다.
    db.commit()
    return taken


def job_to_agent(db, row):
    """중계가 받을 모양으로 바꾼다. 접속 정보는 이 자리에서만 붙는다."""
    out = {"id": row["id"], "kind": row["kind"], "term_id": row["term_id"] or ""}
    try:
        out["payload"] = json.loads(row["payload"] or "{}")
    except ValueError:
        out["payload"] = {}
    # 저장 전 초안 테스트. 비밀번호는 DB 가 아니라 메모리에서 온다.
    draft = take_side("draft", row["id"])
    if draft is not None:
        out["payload"]["draft"] = draft
    if row["server_id"]:
        srv = get_server(db, row["server_id"])
        if srv is None:
            return None
        out["server"] = {"id": srv["id"], "name": srv["name"]}
        out["auth"] = server_auth_for_relay(db, srv)
    return out


def finish_job(db, job_id, ok, result=None, agent_id=None):
    """중계가 올린 결과를 적는다. result 는 요약만 담는다(내용은 넣지 않는다)."""
    sql = ("UPDATE relay_jobs SET state = ?, ok = ?, done_at = ?, result = ?"
           " WHERE id = ? AND state = 'taken'")
    args = ["done" if ok else "failed", 1 if ok else 0, ts(),
            json.dumps(result or {}, ensure_ascii=False)[:2000], job_id]
    if agent_id is not None:
        sql += " AND agent_id = ?"
        args.append(agent_id)
    cur = db.execute(sql, args)
    return cur.rowcount == 1


def get_job(db, job_id):
    return db.execute("SELECT * FROM relay_jobs WHERE id = ?", (job_id,)).fetchone()


def cancel_job(db, job_id, why):
    db.execute(
        "UPDATE relay_jobs SET state = 'canceled', done_at = ?, ok = 0, result = ?"
        " WHERE id = ? AND state IN ('queued','taken')",
        (ts(), json.dumps({"error": why}, ensure_ascii=False), job_id))


def wait_job(db, job_id, timeout, tick=0.15):
    """
    일 하나가 끝날 때까지 기다린다. 끝난 행을 돌려준다. (시간을 넘기면 마지막 상태)

    요청 스레드를 붙잡는 대기다. 그래서 timeout 은 설정값(기본 30초)을 넘지
    않고, 터미널 입력처럼 잦은 일은 이 함수를 쓰지 않는다.
    """
    deadline = time.time() + max(1, timeout)
    while True:
        row = get_job(db, job_id)
        if row is None or row["state"] in ("done", "failed", "canceled"):
            return row
        if time.time() >= deadline:
            return row
        time.sleep(tick)


# ---------------------------------------------------------------------------
# 터미널 (메모리)
# ---------------------------------------------------------------------------
class TermStream(object):
    """
    터미널 하나의 화면과 입력. DB 에 넣지 않는 것만 여기 있다.

    seq 는 1부터 올라가는 조각 번호다. 브라우저는 "내가 본 마지막 번호" 를
    보내고 그 뒤를 받아 간다. 새로고침하면 버퍼에 남아 있는 만큼 다시 그린다.
    """

    def __init__(self, term_id, server_id, user_id):
        self.id = term_id
        self.server_id = server_id
        self.user_id = user_id
        self.cond = threading.Condition()
        self.seq = 0
        self.chunks = collections.deque()     # [(seq, text)]
        self.nbytes = 0
        self.to_agent = []                    # 중계가 가져갈 입력
        self.state = "opening"
        self.close_reason = ""
        self.opened_at = time.time()
        self.last_browser = time.time()
        self.last_agent = 0.0
        self.tail = ""                        # 마지막 출력 꼬리 (비밀번호 프롬프트 판단용)
        self.pending_line = ""                # 기록용. 엔터를 받을 때까지 모은다

    # --- 중계 -> 브라우저 ---------------------------------------------
    def push(self, text):
        if not text:
            return self.seq
        with self.cond:
            self.seq += 1
            self.chunks.append((self.seq, text))
            self.nbytes += len(text)
            self.last_agent = time.time()
            if self.state == "opening":
                self.state = "open"
            limit = config.RELAY_TERM_BUFFER_BYTES
            while self.nbytes > limit and len(self.chunks) > 1:
                _, old = self.chunks.popleft()
                self.nbytes -= len(old)
            self.tail = (self.tail + text)[-200:]
            self.cond.notify_all()
            return self.seq

    def read_after(self, after_seq, wait=0.0):
        """(마지막 seq, 글자, 닫힘 여부, 이유)"""
        with self.cond:
            if self.seq <= after_seq and wait > 0 and self.state != "closed":
                self.cond.wait(wait)
            parts = [t for s, t in self.chunks if s > after_seq]
            first_kept = self.chunks[0][0] if self.chunks else self.seq + 1
            gap = after_seq + 1 < first_kept and after_seq > 0
            self.last_browser = time.time()
            return {
                "seq": self.seq,
                "data": "".join(parts),
                "state": self.state,
                "reason": self.close_reason,
                "dropped": gap,
                "silence": round(time.time() - self.last_agent, 1) if self.last_agent else None,
            }

    # --- 브라우저 -> 중계 ---------------------------------------------
    def send(self, data):
        with self.cond:
            self.to_agent.append(data)
            self.last_browser = time.time()
        wake()

    def take_input(self):
        with self.cond:
            if not self.to_agent:
                return ""
            out, self.to_agent = "".join(self.to_agent), []
            return out

    # --- 기록용 줄 모으기 ---------------------------------------------
    def feed_record(self, data):
        """
        입력에서 "엔터로 끝난 한 줄" 을 골라낸다. 반환: [(줄, 가릴지)]

        제어문자(^C, 화살표 ...)는 줄로 보지 않는다. 비밀번호 프롬프트 뒤에
        온 줄은 내용을 적지 않는다.
        """
        out = []
        with self.cond:
            masked_now = ssh_policy.looks_like_password_prompt(self.tail)
            for ch in data:
                if ch in ("\r", "\n"):
                    line = self.pending_line.strip()
                    self.pending_line = ""
                    if line:
                        out.append(("(가려짐)" if masked_now else line[:500], masked_now))
                elif ch == "\x7f" or ch == "\b":
                    self.pending_line = self.pending_line[:-1]
                elif ch == "\x03" or ch == "\x04":
                    self.pending_line = ""
                elif ord(ch) >= 32:
                    if len(self.pending_line) < 1000:
                        self.pending_line += ch
        return out

    def close(self, reason):
        with self.cond:
            if self.state != "closed":
                self.state = "closed"
                self.close_reason = reason or ""
            self.cond.notify_all()

    def idle_browser(self):
        return time.time() - self.last_browser


class Hub(object):
    def __init__(self):
        self._lock = threading.Lock()
        self._terms = {}

    def create(self, term_id, server_id, user_id):
        t = TermStream(term_id, server_id, user_id)
        with self._lock:
            self._terms[term_id] = t
        return t

    def get(self, term_id):
        with self._lock:
            return self._terms.get(term_id)

    def drop(self, term_id):
        with self._lock:
            return self._terms.pop(term_id, None)

    def open_ids(self):
        with self._lock:
            return [t.id for t in self._terms.values() if t.state != "closed"]

    def all(self):
        with self._lock:
            return list(self._terms.values())

    def pending_input(self):
        """중계가 가져갈 입력. [(term_id, data)]"""
        out = []
        for t in self.all():
            if t.state == "closed":
                continue
            data = t.take_input()
            if data:
                out.append((t.id, data))
        return out

    def has_pending_input(self):
        for t in self.all():
            if t.state != "closed" and t.to_agent:
                return True
        return False


HUB = Hub()


# ---------------------------------------------------------------------------
# 터미널 (DB 쪽)
# ---------------------------------------------------------------------------
def open_term_row(db, term_id, server_id, user_id):
    db.execute(
        "INSERT INTO term_sessions (id, server_id, user_id, state, opened_at, last_io_at)"
        " VALUES (?,?,?,'opening',?,?)", (term_id, server_id, user_id, ts(), ts()))


def term_row(db, term_id):
    return db.execute("SELECT * FROM term_sessions WHERE id = ?", (term_id,)).fetchone()


def set_term_state(db, term_id, state, reason=""):
    if state == "closed":
        db.execute(
            "UPDATE term_sessions SET state = 'closed', closed_at = COALESCE(closed_at, ?),"
            " close_reason = CASE WHEN close_reason = '' THEN ? ELSE close_reason END"
            " WHERE id = ?", (ts(), (reason or "")[:200], term_id))
    else:
        db.execute("UPDATE term_sessions SET state = ?, last_io_at = ? WHERE id = ?",
                   (state, ts(), term_id))


def record_term_lines(db, term_id, lines):
    """사람이 친 줄을 기록에 남긴다. 등급은 매기지 않는다."""
    if not lines:
        return
    db.executemany(
        "INSERT INTO term_inputs (term_id, line, created_at) VALUES (?,?,?)",
        [(term_id, line, ts()) for line, _masked in lines])
    db.execute("UPDATE term_sessions SET lines_in = lines_in + ?, last_io_at = ?"
               " WHERE id = ?", (len(lines), ts(), term_id))


def open_term_count(db, user_id):
    return db.execute(
        "SELECT COUNT(*) AS c FROM term_sessions"
        " WHERE user_id = ? AND state IN ('opening','open')", (user_id,)).fetchone()["c"]


def open_terms_for_user(db, user_id):
    return db.execute(
        "SELECT t.*, s.name AS server_name FROM term_sessions t"
        " JOIN ssh_servers s ON s.id = t.server_id"
        " WHERE t.user_id = ? AND t.state IN ('opening','open')"
        " ORDER BY t.opened_at", (user_id,)).fetchall()


# ---------------------------------------------------------------------------
# 명령 기록 (챗봇이 고른 것만)
# ---------------------------------------------------------------------------
def create_command(db, server_id, session_id, message_id, user_id, command,
                   level, state, reason="", source="chat"):
    cur = db.execute(
        "INSERT INTO ssh_commands (server_id, session_id, message_id, user_id, source,"
        " command, level, state, reason, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (server_id, session_id, message_id, user_id, source, command[:2000],
         level, state, (reason or "")[:300], ts()))
    return cur.lastrowid


def get_command(db, cmd_id):
    return db.execute("SELECT * FROM ssh_commands WHERE id = ?", (cmd_id,)).fetchone()


def command_payload(db, row, now=None):
    """승인 카드가 쓰는 모양."""
    d = row_to_dict(row)
    srv = get_server(db, row["server_id"]) if row["server_id"] else None
    d["server_name"] = srv["name"] if srv else ""
    d["level_label"] = ssh_policy.LEVEL_LABELS.get(row["level"], row["level"])
    d["approver_name"] = ""
    if row["approved_by"]:
        u = db.execute("SELECT username, display_name FROM users WHERE id = ?",
                       (row["approved_by"],)).fetchone()
        if u:
            d["approver_name"] = u["display_name"] or u["username"]
    if row["state"] == "pending":
        d["expires_in"] = _pending_left(db, row)
    return d


def _pending_left(db, row):
    """승인 카드에 남은 시간(초). DB 의 시각 문자열은 ts() 와 같은 형식이다."""
    try:
        made = time.mktime(time.strptime(row["created_at"], "%Y-%m-%d %H:%M:%S"))
    except (TypeError, ValueError):
        return 0
    return max(0, int(made + approval_seconds(db) - time.time()))


def commands_for_messages(db, message_ids):
    """메시지 id -> 승인 카드 목록."""
    if not message_ids:
        return {}
    marks = ",".join("?" for _ in message_ids)
    rows = db.execute(
        "SELECT * FROM ssh_commands WHERE message_id IN (%s) ORDER BY id" % marks,
        list(message_ids)).fetchall()
    out = {}
    for r in rows:
        out.setdefault(r["message_id"], []).append(command_payload(db, r))
    return out


def expire_pending(db):
    """승인 시간을 넘긴 카드를 취소한다. 반환: 취소한 개수."""
    cur = db.execute(
        "UPDATE ssh_commands SET state = 'expired', finished_at = ?,"
        " reason = CASE WHEN reason = '' THEN '승인 시간이 지나 취소되었습니다' ELSE reason END"
        " WHERE state = 'pending'"
        "   AND created_at < datetime('now','localtime','-%d seconds')"
        % approval_seconds(db), (ts(),))
    return cur.rowcount


# ---------------------------------------------------------------------------
# 청소
# ---------------------------------------------------------------------------
def close_orphan_terms(db):
    """
    기동할 때 한 번. 지난 프로세스가 남긴 '열린' 터미널을 닫는다.

    터미널의 화면 버퍼는 메모리(HUB)에만 있다. 프로세스가 다시 뜨면 그 버퍼는
    비어 있으므로 DB 에 'open' 으로 남은 줄은 **살아 있을 수 없는 줄**이다.
    그대로 두면 "한 사람 2개" 자리를 유령이 차지해서 다음에 터미널을 못 연다.
    """
    rows = db.execute(
        "SELECT id FROM term_sessions WHERE state IN ('opening','open')").fetchall()
    for r in rows:
        set_term_state(db, r["id"], "closed", "서버가 다시 시작되어 닫혔습니다")
    if rows:
        db.commit()
    return len(rows)


def housekeep(db):
    """
    10초마다 한 번. 하는 일은 넷이다.

      1. 승인 시간이 지난 카드를 취소한다.
      2. 브라우저가 조용해진 터미널을 닫는다. (유령 ssh 가 쌓이면 다음 사람이
         붙지 못한다)
      3. 중계가 안 붙어 있는데 큐에 쌓인 일을 취소한다. (화면이 영원히 돌지
         않게)
      4. 끝난 전송 큐를 보관 기간 뒤에 지운다. **기록은 지우지 않는다.**
    """
    steps = []
    n = expire_pending(db)
    if n:
        steps.append("승인 시간 초과 %d건" % n)

    idle = term_idle_seconds(db)
    closed = []
    for t in HUB.all():
        if t.state == "closed":
            # 브라우저가 마지막 화면을 받아 갈 시간을 준 뒤에 버린다
            if t.idle_browser() > 60:
                HUB.drop(t.id)
            continue
        if t.idle_browser() > idle:
            t.close("%d초 동안 화면을 보는 사람이 없어 닫았습니다" % idle)
            closed.append((t.id, t.user_id))
    for term_id, owner in closed:
        set_term_state(db, term_id, "closed", "브라우저가 조용해져 닫음")
        enqueue(db, KIND_TERM_CLOSE, owner, term_id=term_id)
        steps.append("조용한 터미널 닫기 %s" % term_id[:8])

    # 중계가 붙어 있는 사람들. 이 사람들의 일만 살아 있을 수 있다.
    LIVE = ("SELECT owner_id FROM relay_agents WHERE revoked_at IS NULL"
            "   AND owner_id IS NOT NULL"
            "   AND last_seen_at >= datetime('now','localtime','-90 seconds')")

    cur = db.execute(
        "UPDATE relay_jobs SET state = 'canceled', ok = 0, done_at = ?, result = ?"
        " WHERE state IN ('queued','taken')"
        "   AND created_at < datetime('now','localtime','-60 seconds')"
        "   AND (owner_id IS NULL OR owner_id NOT IN (%s))" % LIVE,
        (ts(), json.dumps({"error": "내 중계가 붙어 있지 않습니다"},
                          ensure_ascii=False)))
    if cur.rowcount:
        steps.append("중계 없음으로 취소 %d건" % cur.rowcount)

    # 중계가 끊긴 사람의 터미널은 열려 있을 수 없다. 남의 터미널은 건드리지 않는다.
    rows = db.execute(
        "SELECT id FROM term_sessions WHERE state IN ('opening','open')"
        "   AND (user_id IS NULL OR user_id NOT IN (%s))" % LIVE).fetchall()
    for r in rows:
        t = HUB.get(r["id"])
        if t:
            t.close("중계와 연결이 끊어졌습니다")
        set_term_state(db, r["id"], "closed", "중계 연결 끊김")
    if rows:
        steps.append("중계 끊김으로 터미널 %d개 닫음" % len(rows))

    cur = db.execute(
        "DELETE FROM relay_jobs WHERE state IN ('done','failed','canceled')"
        "   AND created_at < datetime('now','localtime','-%d days')" % queue_keep_days(db))
    if cur.rowcount:
        steps.append("오래된 전송 큐 %d건 정리" % cur.rowcount)

    n = sweep_side()
    if n:
        steps.append("아무도 받아 가지 않은 결과 %d건 버림" % n)

    db.execute("UPDATE relay_enroll_codes SET used_at = ?"
               " WHERE used_at IS NULL AND expires_at <= datetime('now','localtime')",
               (ts(),))
    db.commit()
    return steps


# ---------------------------------------------------------------------------
# 중계 프로그램 (relay.exe)
#
# 사람마다 자기 VDI 에 깔아야 한다. 그래서 받을 수 있는 자리가 웹에 있어야 한다.
# 관리자가 한 번 올리고, 쓰는 사람이 서버 화면에서 받는다.
#
# 파일은 데이터 루트 아래(RELAY_DIR)에 둔다. 소스 트리에 바이너리를 넣지
# 않는다. git 에 10MB 짜리 exe 가 버전마다 쌓이면 clone 이 무거워지고, 무엇보다
# **빌드 산출물은 소스가 아니다.**
#
# 메타데이터(이름/크기/해시/올린 사람)는 파일 옆 program.json 에 둔다. 파일과
# 그 설명이 같이 움직여야 백업/복구에서 어긋나지 않는다.
# ---------------------------------------------------------------------------
PROGRAM_META = "program.json"

# 올릴 수 있는 확장자. 받는 사람이 그대로 실행하는 파일이라 좁게 둔다.
PROGRAM_EXTS = (".exe", ".py", ".zip")

# 프로그램은 두 가지다. 서로 다른 PC 로 가는 다른 프로그램이라 칸을 나눈다.
#   relay  : VDI 안에서 도는 중계 (relay.exe + plink.exe)
#   client : 사용자의 바깥 PC 에서 도는 클라이언트 (claude-term.exe + putty.exe)
# relay 의 파일 이름은 예전 그대로 둔다. 이미 올려 둔 것이 그대로 보여야 한다.
PROGRAM_KINDS = {
    "relay": {"stem": "relay", "meta": PROGRAM_META},
    "client": {"stem": "client", "meta": "client-program.json"},
}


def program_dir():
    return config.RELAY_DIR


def _kind(kind):
    if kind not in PROGRAM_KINDS:
        raise ValueError("모르는 프로그램 종류입니다.")
    return PROGRAM_KINDS[kind]


def _program_meta_path(kind="relay"):
    return os.path.join(program_dir(), _kind(kind)["meta"])


def program_info(kind="relay"):
    """지금 올라와 있는 프로그램의 설명. 없으면 None."""
    try:
        with open(_program_meta_path(kind), "r", encoding="utf-8") as f:
            meta = json.load(f)
    except (IOError, OSError, ValueError):
        return None
    path = os.path.join(program_dir(), meta.get("stored") or "")
    if not meta.get("stored") or not os.path.exists(path):
        return None
    meta["path"] = path
    return meta


def save_program(fileobj, filename, user_id, username, kind="relay"):
    """
    프로그램을 바꾼다. 반환: 설명 dict

    먼저 임시 이름으로 받아 두고 다 받은 뒤에 자리를 바꾼다. 받다가 끊겼을 때
    반쯤 쓰인 파일이 사람들에게 내려가면 안 된다.
    """
    ext = os.path.splitext(filename or "")[1].lower()
    if ext not in PROGRAM_EXTS:
        raise ValueError("올릴 수 있는 것은 %s 입니다." % ", ".join(PROGRAM_EXTS))

    os.makedirs(program_dir(), exist_ok=True)
    stored = _kind(kind)["stem"] + ext
    tmp = os.path.join(program_dir(), stored + ".part")
    limit = config.RELAY_PROGRAM_MAX_MB * 1024 * 1024

    digest = hashlib.sha256()
    size = 0
    with open(tmp, "wb") as out:
        while True:
            chunk = fileobj.read(262144)
            if not chunk:
                break
            size += len(chunk)
            if size > limit:
                out.close()
                os.remove(tmp)
                raise ValueError("파일이 너무 큽니다. (최대 %dMB)"
                                 % config.RELAY_PROGRAM_MAX_MB)
            digest.update(chunk)
            out.write(chunk)
    if not size:
        os.remove(tmp)
        raise ValueError("빈 파일입니다.")

    final = os.path.join(program_dir(), stored)
    # os.replace 는 한 번에 바꾼다. 지우고 옮기면 그 사이에 파일이 없는 순간이
    # 생겨서, 바로 그때 받는 사람이 404 를 본다. 리눅스에서는 이미 받고 있던
    # 사람도 끊기지 않는다(헌 파일을 계속 읽는다).
    try:
        os.replace(tmp, final)
    except OSError as exc:
        # 윈도우에서 방금 누가 받아 가 핸들이 열려 있으면 여기로 온다.
        # 리눅스 운영에서는 나지 않는다. 500 으로 올리지 않고 이유를 말한다.
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise ValueError("파일을 바꾸지 못했습니다. 누군가 받고 있는 중일 수 "
                         "있습니다. 잠시 뒤에 다시 올려 주세요. (%s)" % exc)

    meta = {
        "name": os.path.basename(filename)[:120],
        "stored": stored,
        "size": size,
        "sha256": digest.hexdigest(),
        "uploaded_at": ts(),
        "uploaded_by": username,
        "uploaded_by_id": user_id,
    }
    with open(_program_meta_path(kind), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    meta["path"] = final
    return meta


def remove_program(kind="relay"):
    """올려 둔 프로그램을 치운다. 반환: 지웠는지 여부"""
    meta = program_info(kind)
    if meta is None:
        return False
    for path in (meta["path"], _program_meta_path(kind)):
        try:
            os.remove(path)
        except OSError:
            pass
    return True


def close_user_terms(db, user_id, why):
    """
    그 사람의 열린 터미널을 그 자리에서 닫는다.

    허용을 떼거나 중계 등록을 해제할 때 쓴다. 다음 청소 시각까지 기다리면
    권한이 사라진 뒤에도 몇 분 동안 화면이 살아 있다.
    """
    rows = db.execute(
        "SELECT id FROM term_sessions WHERE user_id = ? AND state IN ('opening','open')",
        (user_id,)).fetchall()
    for r in rows:
        stream = HUB.get(r["id"])
        if stream:
            stream.close(why)
        set_term_state(db, r["id"], "closed", why)
        enqueue(db, KIND_TERM_CLOSE, user_id, term_id=r["id"])
    return len(rows)
