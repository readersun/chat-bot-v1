#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SSH 중계 통합 시험.

진짜 ssh 를 띄우지 않는다. 대신 **가짜 중계**를 스레드로 돌린다. 가짜 중계는
진짜와 똑같은 API 를 쓴다. (POST /api/relay/poll -> 처리 -> POST /api/relay/result)
그래서 이 시험은 "서버가 중계와 주고받는 규약" 을 그대로 검사한다.

Claude 도 부르지 않는다. 미리 정한 답을 돌려주는 가짜 provider 를 끼운다.
그 답에 ```ssh 블록을 넣어 두면 챗봇이 명령을 고른 상황이 된다.

    python -m unittest tests.test_relay_flow
"""

import atexit
import io
import json
import os
import re
import shutil
import sys
import tempfile
import threading
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# app 을 import 하면 그 자리에서 마이그레이션이 돈다. 그 전에 임시 DB 를 잡아야
# 한다. (config 가 import 시점에 환경변수를 읽는다)
_TMP = tempfile.mkdtemp(prefix="relay-test-")
os.environ["DATABASE_PATH"] = os.path.join(_TMP, "chat.db")
os.environ["UPLOAD_DIR"] = os.path.join(_TMP, "uploads")
os.environ["NOTES_DIR"] = os.path.join(_TMP, "notes")
os.environ["BACKUP_DIR"] = os.path.join(_TMP, "backups")
os.environ["SECRET_KEY"] = "test-secret-key-for-relay-flow"
os.environ["PATCH_SCAN_ENABLED"] = "0"

# 임시 폴더는 **프로세스가 끝날 때** 치운다.
#
# tearDownModule 에서 지우면 안 된다. 시험 모듈을 한 프로세스에서 함께 돌리면
# (python -m unittest discover) config 는 먼저 import 된 쪽의 DATABASE_PATH 를
# 들고 있다. 먼저 끝난 모듈이 자기 폴더라고 지워 버리면, 같은 DB 를 쓰는 다음
# 모듈이 DB 를 잃고 전부 깨진다.
atexit.register(lambda: shutil.rmtree(_TMP, ignore_errors=True))

import app as app_module                                      # noqa: E402
import auth                                                   # noqa: E402
import db as db_module                                        # noqa: E402
import permissions                                            # noqa: E402
import relay_store as store                                   # noqa: E402
import settings_store                                         # noqa: E402

APP = app_module.app

# 챗봇이 명령을 고른 모양. 백틱을 소스에 직접 쓰면 읽기 어려워 상수로 둔다.
FENCE = "```ssh" + chr(10) + "%s" + chr(10) + "```"


# ---------------------------------------------------------------------------
# 가짜 provider : Claude 를 부르지 않는다
# ---------------------------------------------------------------------------
class FakeProvider(object):
    """send() 를 부를 때마다 미리 넣어 둔 답을 하나씩 돌려준다."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.asked = []

    def supports_resume(self):
        return False

    def send(self, question, images=(), history=(), resume_id=None, new_session_id=None):
        self.asked.append(question)
        if not self.replies:
            return {"ok": True, "text": "(더 할 말이 없습니다)", "session_id": None}
        text = self.replies.pop(0)
        if isinstance(text, Exception):
            return {"ok": False, "text": str(text), "session_id": None}
        return {"ok": True, "text": text, "session_id": None}


# ---------------------------------------------------------------------------
# 가짜 중계 : 진짜와 같은 API 를 쓴다
# ---------------------------------------------------------------------------
class FakeRelay(threading.Thread):
    """
    poll -> 일 처리 -> result 를 반복한다.

    ssh 를 띄우지 않고, 명령마다 미리 정한 출력을 돌려준다. 터미널은 받은
    입력을 그대로 되돌려 보낸다(에코). 진짜 셸이 하는 일과 같은 모양이다.
    """

    def __init__(self, key, outputs=None, fail_test=False):
        super(FakeRelay, self).__init__(name="fake-relay", daemon=True)
        self.key = key
        self.outputs = outputs or {}
        self.fail_test = fail_test
        self.stop = threading.Event()
        self.ran = []                 # 실제로 돌린 명령
        self.auth_seen = []           # 받은 접속 정보 (비밀이 실려 오는지 확인)
        self.payloads = []            # 받은 payload 전체
        self.open_terms = set()
        self.client = APP.test_client()
        self.ready = threading.Event()

    def _post(self, path, payload):
        return self.client.post(path, json=payload,
                                headers={"X-Relay-Key": self.key})

    def run(self):
        self.ready.set()
        while not self.stop.is_set():
            res = self._post("/api/relay/poll", {})
            if res.status_code != 200:
                time.sleep(0.05)
                continue
            data = res.get_json()
            jobs, inputs = data.get("jobs") or [], data.get("input") or []
            out = {"jobs": [], "term": []}

            for item in inputs:
                # 터미널 에코. 진짜 셸처럼 받은 글자를 그대로 돌려준다.
                out["term"].append({"term_id": item["term_id"],
                                    "data": item["data"]})

            for job in jobs:
                self.payloads.append(job.get("payload") or {})
                if job.get("auth"):
                    self.auth_seen.append(job["auth"])
                draft = (job.get("payload") or {}).get("draft")
                if draft:
                    self.auth_seen.append(draft)
                kind = job["kind"]
                if kind == "test":
                    out["jobs"].append({
                        "id": job["id"], "ok": not self.fail_test,
                        "message": ("연결 확인 · 0.42초" if not self.fail_test
                                    else "연결 실패 · Connection timed out"),
                        "banner": "SSH-2.0-OpenSSH_8.0p1", "elapsed": 0.42,
                        "error": "" if not self.fail_test else "Connection timed out"})
                elif kind == "run":
                    cmd = (job.get("payload") or {}).get("command") or ""
                    self.ran.append(cmd)
                    body = self.outputs.get(cmd, "OK\n")
                    out["jobs"].append({"id": job["id"], "ok": True, "exit_code": 0,
                                        "output": body, "elapsed": 0.21})
                elif kind == "term_open":
                    self.open_terms.add(job["term_id"])
                    out["jobs"].append({"id": job["id"], "ok": True,
                                        "message": "열었습니다"})
                    out["term"].append({"term_id": job["term_id"],
                                        "data": "[svc_ops@nfs-181 ~]$ "})
                elif kind == "term_close":
                    self.open_terms.discard(job["term_id"])
                    out["jobs"].append({"id": job["id"], "ok": True})
                    out["term"].append({"term_id": job["term_id"], "data": "",
                                        "closed": True, "reason": "닫았습니다"})
                else:
                    out["jobs"].append({"id": job["id"], "ok": False,
                                        "error": "모르는 일"})

            if out["jobs"] or out["term"]:
                self._post("/api/relay/result", out)


# ---------------------------------------------------------------------------
# 공통
# ---------------------------------------------------------------------------
def conn():
    return db_module.connect()


def set_setting(key, value):
    c = conn()
    try:
        c.execute("INSERT INTO settings (key, value, updated_at) VALUES (?,?,?) "
                  "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                  (key, str(value), db_module.ts()))
        c.commit()
    finally:
        c.close()
    settings_store.invalidate()


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        c = conn()
        try:
            if not auth.user_by_name(c, "admin"):
                auth.create_user(c, "admin", "admin-pw-12345", "관리자", role="admin")
            if not auth.user_by_name(c, "hong"):
                uid = auth.create_user(c, "hong", "hong-pw-12345", "홍길동")
                permissions.set_user_menus(c, uid, ["chat"], None)
            if not auth.user_by_name(c, "kim"):
                uid = auth.create_user(c, "kim", "kim-pw-12345", "김운영")
                permissions.set_user_menus(c, uid, ["chat"], None)
            c.execute("INSERT OR IGNORE INTO projects (id, name, description,"
                      " created_at, updated_at) VALUES (1,'기본','',?,?)",
                      (db_module.ts(), db_module.ts()))
            c.commit()
        finally:
            c.close()
        # 기본 정책은 가장 조용한 쪽이다. 시험마다 필요하면 올린다.
        set_setting("relay_policy", "read")
        set_setting("relay_poll_seconds", "5")
        # 시험에서는 짧게. 긴 대기는 시험을 느리게만 한다.
        set_setting("relay_poll_seconds", "1")
        set_setting("relay_run_timeout", "10")
        set_setting("relay_approval_seconds", "120")

    def setUp(self):
        self.c = APP.test_client()
        self.relays = []
        self.reset()

    @staticmethod
    def reset():
        """
        시험마다 깨끗한 상태에서 시작한다.

        사용자와 프로젝트는 남기고 중계/서버/대화만 비운다. 앞 시험이 남긴
        서버 이름 때문에 뒤 시험이 409 로 죽는 것을 막는다.
        """
        for term in store.HUB.all():
            store.HUB.drop(term.id)
        c = conn()
        try:
            # 대화가 붙어 있는 서버는 그냥 지워지지 않는다(ON DELETE 규칙 없음).
            # 실제 삭제 경로와 같은 순서로 떼어 낸 뒤 지운다.
            c.execute("UPDATE sessions SET server_id = NULL")
            for t in ("ssh_commands", "term_inputs", "term_sessions", "relay_jobs",
                      "relay_enroll_codes", "relay_agents", "ssh_grants",
                      "ssh_servers", "messages", "sessions"):
                c.execute("DELETE FROM %s" % t)
            c.execute("UPDATE users SET ssh_level = 'off', ssh_all_servers = 0")
            c.execute("DELETE FROM user_menus WHERE menu_key = 'servers'")
            c.commit()
        finally:
            c.close()
        store.remove_program()
        c = conn()
        try:
            pass
        finally:
            c.close()

    def tearDown(self):
        for r in self.relays:
            r.stop.set()
        for r in self.relays:
            r.join(timeout=12)

    @property
    def relay(self):
        """맨 처음 띄운 중계. 시험 본문이 ran/auth_seen 을 볼 때 쓴다."""
        return self.relays[0] if self.relays else None

    # --- 도우미 ----------------------------------------------------
    def login(self, username, password):
        """
        진짜 로그인 화면과 같은 길로 들어간다.

        /login 도 CSRF 검사를 받는다. 그래서 화면을 먼저 받아 숨은 토큰을
        꺼내고, 그것을 form 에 넣어 보낸다. 이 과정을 건너뛰면 시험은
        통과하는데 실제 화면은 안 되는 상태를 못 잡는다.
        """
        # 사람이 바뀌면 브라우저도 바뀐다. 쿠키를 물려받으면 /login 이
        # "이미 로그인했다" 며 돌려보내서 CSRF 토큰을 못 받는다.
        self.c = APP.test_client()
        page = self.c.get("/login").get_data(as_text=True)
        m = re.search(r'name="_csrf" value="([^"]+)"', page)
        self.assertTrue(m, "로그인 화면에 CSRF 토큰이 없다")
        res = self.c.post("/login", data={"username": username,
                                          "password": password,
                                          "_csrf": m.group(1)},
                          follow_redirects=False)
        self.assertIn(res.status_code, (200, 302),
                      "로그인 실패: %s %s" % (res.status_code,
                                             res.get_data(as_text=True)[:200]))
        self.csrf = self.c.get("/api/me").get_json()["csrf_token"]
        return self.csrf

    def j(self, method, url, body=None):
        kw = {"headers": {"X-CSRF-Token": getattr(self, "csrf", "")}}
        if body is not None:
            kw["json"] = body
        res = getattr(self.c, method.lower())(url, **kw)
        return res.status_code, (res.get_json() or {})

    def start_relay(self, **kw):
        """
        **지금 로그인한 사람**의 등록 코드를 받아 그 사람의 중계를 띄운다.

        사람마다 자기 중계를 가진다. 그래서 어느 사람으로 로그인한 뒤에
        부르는지가 중요하다.
        """
        code = self.j("POST", "/api/my-relay/enroll", {})[1]["code"]
        res = self.c.post("/api/relay/register",
                          json={"code": code, "name": "VDI-OPS-01",
                                "version": "0.1.0", "os": "nt win32"})
        self.assertEqual(res.status_code, 201, res.get_data(as_text=True))
        key = res.get_json()["agent_key"]
        relay = FakeRelay(key, **kw)
        relay.start()
        relay.ready.wait(3)
        self.relays.append(relay)
        time.sleep(0.2)
        return key

    def make_server(self, name="nfs-181", host="10.20.30.181", auth_kind="key",
                    key_name="ops-vdi-01", password=None):
        body = {"name": name, "host": host, "port": 22, "username": "svc_ops",
                "auth_kind": auth_kind, "key_name": key_name,
                "description": "NFS 1차. /data 마운트."}
        if password:
            body["password"] = password
        code, data = self.j("POST", "/api/servers", body)
        self.assertEqual(code, 201, data)
        return data["server"]

    def grant(self, username, level="read", all_servers=True, server_ids=()):
        c = conn()
        try:
            uid = auth.user_by_name(c, username)["id"]
        finally:
            c.close()
        code, data = self.j("PUT", "/api/admin/relay/grants/%d" % uid,
                            {"level": level, "all_servers": all_servers,
                             "server_ids": list(server_ids)})
        self.assertEqual(code, 200, data)
        return uid


# ---------------------------------------------------------------------------
# 1. 등록
# ---------------------------------------------------------------------------
class TestEnroll(Base):
    def test_register_requires_valid_code(self):
        self.login("admin", "admin-pw-12345")
        res = self.c.post("/api/relay/register", json={"code": "000000"})
        self.assertEqual(res.status_code, 403)

    def test_code_is_single_use(self):
        self.login("admin", "admin-pw-12345")
        code = self.j("POST", "/api/my-relay/enroll", {})[1]["code"]
        first = self.c.post("/api/relay/register", json={"code": code, "name": "a"})
        self.assertEqual(first.status_code, 201)
        again = self.c.post("/api/relay/register", json={"code": code, "name": "b"})
        self.assertEqual(again.status_code, 403, "같은 코드로 두 번 등록되면 안 된다")

    def test_new_code_kills_old_code(self):
        self.login("admin", "admin-pw-12345")
        old = self.j("POST", "/api/my-relay/enroll", {})[1]["code"]
        self.j("POST", "/api/my-relay/enroll", {})
        res = self.c.post("/api/relay/register", json={"code": old})
        self.assertEqual(res.status_code, 403)

    def test_token_is_not_stored_in_plain(self):
        self.login("admin", "admin-pw-12345")
        key = self.start_relay()
        c = conn()
        try:
            rows = c.execute("SELECT key_hash FROM relay_agents").fetchall()
        finally:
            c.close()
        for r in rows:
            self.assertNotEqual(r["key_hash"], key)
            self.assertEqual(len(r["key_hash"]), 64)

    def test_relay_api_needs_key(self):
        res = self.c.post("/api/relay/poll", json={})
        self.assertEqual(res.status_code, 401)
        res = self.c.post("/api/relay/result", json={}, headers={"X-Relay-Key": "x"})
        self.assertEqual(res.status_code, 401)

    def test_relay_api_does_not_need_csrf(self):
        """중계는 쿠키를 쓰지 않는다. CSRF 토큰을 요구하면 로그인을 해야 한다."""
        self.login("admin", "admin-pw-12345")
        key = self.start_relay()
        fresh = APP.test_client()         # 쿠키도 CSRF 도 없는 클라이언트
        res = fresh.post("/api/relay/poll", json={}, headers={"X-Relay-Key": key})
        self.assertEqual(res.status_code, 200)


# ---------------------------------------------------------------------------
# 1.5 사람마다 중계 한 대
# ---------------------------------------------------------------------------
class TestPerUserRelay(Base):
    """
    사람마다 자기 VDI 에 중계를 깐다. 그래서 두 가지가 지켜져야 한다.

      1. 내 중계는 **내 일만** 가져간다. 남의 일을 가져가면 그 명령이 누구
         권한으로 돌았는지가 흐려진다.
      2. 내가 다시 등록해도 **남의 중계는 끊기지 않는다.** 한 사람이 VDI 를
         바꿀 때마다 전원이 멈추면 쓸 수 없다.
    """

    def test_my_relay_does_not_take_other_peoples_work(self):
        # 관리자 중계만 띄우고, 홍길동에게 서버를 허용한다
        self.login("admin", "admin-pw-12345")
        admin_key = self.start_relay()
        srv = self.make_server(name="mine-only", host="10.7.0.1")
        self.grant("hong", level="read", all_servers=True)

        # 홍길동은 자기 중계가 없다 -> 503 이고, 관리자 중계가 대신하지 않는다
        self.login("hong", "hong-pw-12345")
        code, data = self.j("POST", "/api/term", {"server_id": srv["id"]})
        self.assertEqual(code, 503, data)
        self.assertIn("내 VDI", data.get("error", ""))

        code, data = self.j("POST", "/api/servers/%d/test" % srv["id"])
        self.assertEqual(code, 503, data)

        # 관리자 중계는 홍길동의 일을 하나도 보지 못했다
        time.sleep(0.5)
        self.assertEqual(self.relay.ran, [])
        self.assertEqual(self.relay.open_terms, set())
        self.assertTrue(admin_key)

    def test_each_person_gets_their_own_work(self):
        self.login("admin", "admin-pw-12345")
        self.start_relay()
        srv = self.make_server(name="shared-host", host="10.7.0.2")
        self.grant("hong", level="read", all_servers=True)

        self.login("hong", "hong-pw-12345")
        self.start_relay()                      # 홍길동 중계 (relays[1])
        hong_relay = self.relays[-1]

        code, data = self.j("POST", "/api/term", {"server_id": srv["id"]})
        self.assertEqual(code, 201, data)
        term_id = data["term_id"]

        # 홍길동 중계가 열었고, 관리자 중계는 모른다
        deadline = time.time() + 6
        while time.time() < deadline and term_id not in hong_relay.open_terms:
            time.sleep(0.1)
        self.assertIn(term_id, hong_relay.open_terms)
        self.assertNotIn(term_id, self.relays[0].open_terms)
        self.j("POST", "/api/term/%s/close" % term_id)

    def test_registering_again_does_not_kick_others(self):
        self.login("admin", "admin-pw-12345")
        self.start_relay()
        self.login("hong", "hong-pw-12345")
        self.grant_self_menu()
        self.start_relay()

        # 홍길동이 VDI 를 다시 깔아 재등록한다
        self.start_relay()

        c = conn()
        try:
            admin_id = auth.user_by_name(c, "admin")["id"]
            hong_id = auth.user_by_name(c, "hong")["id"]
            live_admin = c.execute(
                "SELECT COUNT(*) AS n FROM relay_agents"
                " WHERE owner_id = ? AND revoked_at IS NULL", (admin_id,)
            ).fetchone()["n"]
            live_hong = c.execute(
                "SELECT COUNT(*) AS n FROM relay_agents"
                " WHERE owner_id = ? AND revoked_at IS NULL", (hong_id,)
            ).fetchone()["n"]
        finally:
            c.close()
        self.assertEqual(live_admin, 1, "남의 중계가 끊겼다")
        self.assertEqual(live_hong, 1, "한 사람에 한 대여야 한다")

    def grant_self_menu(self):
        """홍길동에게 서버 메뉴를 준다. (등록 코드를 받으려면 필요하다)"""
        me = self.c
        self.login("admin", "admin-pw-12345")
        self.grant("hong", level="read", all_servers=True)
        self.login("hong", "hong-pw-12345")
        return me


# ---------------------------------------------------------------------------
# 1.6 중계 프로그램 배포
# ---------------------------------------------------------------------------
class TestProgram(Base):
    """
    쓰는 사람이 자기 VDI 에 깔아야 하므로, 받을 수 있는 자리가 웹에 있어야 한다.
    올리는 것은 관리자만이다. 아무나 올릴 수 있으면 그 자체가 사내에 프로그램을
    뿌리는 길이 된다.
    """

    BODY = b"MZ\x90\x00fake relay program\n" * 50

    def upload(self, name="relay.exe", body=None):
        return self.c.post(
            "/api/admin/relay/program",
            data={"file": (io.BytesIO(body or self.BODY), name)},
            content_type="multipart/form-data",
            headers={"X-CSRF-Token": self.csrf})

    def test_none_uploaded_means_404(self):
        self.login("admin", "admin-pw-12345")
        store.remove_program()
        res = self.c.get("/servers/program")
        self.assertEqual(res.status_code, 404)
        code, data = self.j("GET", "/api/my-relay")
        self.assertFalse(data["relay"]["program"]["available"])

    def test_admin_uploads_user_downloads(self):
        self.login("admin", "admin-pw-12345")
        res = self.upload()
        self.assertEqual(res.status_code, 200, res.get_data(as_text=True))
        meta = res.get_json()["program"]
        self.assertEqual(meta["name"], "relay.exe")
        self.assertEqual(meta["size"], len(self.BODY))

        # 받는 쪽은 서버 메뉴만 있으면 된다
        self.login("admin", "admin-pw-12345")
        self.grant("hong", level="read", all_servers=True)
        self.login("hong", "hong-pw-12345")
        res = self.c.get("/servers/program")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.get_data(), self.BODY, "받은 내용이 올린 것과 다르다")
        self.assertIn("attachment", res.headers.get("Content-Disposition", ""))

        code, data = self.j("GET", "/api/my-relay")
        prog = data["relay"]["program"]
        self.assertTrue(prog["available"])
        self.assertEqual(prog["sha256"], meta["sha256"])

    def test_only_admin_uploads(self):
        self.login("admin", "admin-pw-12345")
        self.grant("hong", level="read", all_servers=True)
        self.login("hong", "hong-pw-12345")
        res = self.upload()
        self.assertEqual(res.status_code, 403)

    def test_bad_extension_is_refused(self):
        self.login("admin", "admin-pw-12345")
        res = self.upload(name="relay.bat")
        self.assertEqual(res.status_code, 400)
        self.assertIn("올릴 수 있는", res.get_json().get("error", ""))

    def test_no_relay_menu_no_download(self):
        self.login("admin", "admin-pw-12345")
        self.upload()
        self.login("kim", "kim-pw-12345")        # 서버 메뉴 없음
        res = self.c.get("/servers/program")
        self.assertEqual(res.status_code, 403)

    def test_replacing_the_program_is_one_step(self):
        """
        바꿔 올리면 **받는 사람이 404 를 보는 순간이 없어야 한다.**

        전에는 `os.remove(final)` 뒤에 `os.rename` 이었다. 그 사이에 파일이
        없는 순간이 생긴다. 바로 그때 받는 사람은 "프로그램이 없다" 는 404 를
        본다. 윈도우에서는 방금 누가 받아 가 핸들이 열려 있으면 remove 가
        터져서 500 까지 났다. 지금은 os.replace 한 번이다.
        """
        self.login("admin", "admin-pw-12345")
        v1 = b"MZ-version-ONE"
        v2 = b"MZ-version-TWO-longer"
        self.assertEqual(self.upload(body=v1).status_code, 200)

        res = self.c.get("/servers/program")
        self.assertEqual(res.get_data(), v1)
        res.close()

        res = self.upload(body=v2)
        self.assertEqual(res.status_code, 200, res.get_data(as_text=True))
        self.assertEqual(res.get_json()["program"]["size"], len(v2))

        res = self.c.get("/servers/program")
        self.assertEqual(res.get_data(), v2, "바꿔 올렸는데 헌 파일이 내려왔다")
        res.close()

    def test_failed_replace_keeps_the_old_file_and_says_why(self):
        """
        바꾸다 실패하면 **헌 파일이 그대로 남아야** 하고, 500 이 아니라
        왜 안 됐는지 말해야 한다. (윈도우에서 누가 받고 있는 경우)
        """
        self.login("admin", "admin-pw-12345")
        v1 = b"MZ-keep-me"
        self.assertEqual(self.upload(body=v1).status_code, 200)

        real = os.replace

        def boom(src, dst):
            raise OSError(32, "다른 프로세스가 파일을 사용 중입니다")

        os.replace = boom
        try:
            res = self.upload(body=b"MZ-should-not-land")
            self.assertEqual(res.status_code, 400, res.get_data(as_text=True))
            self.assertIn("받고 있는 중", res.get_json().get("error", ""))
        finally:
            os.replace = real

        res = self.c.get("/servers/program")
        self.assertEqual(res.get_data(), v1, "실패했는데 헌 파일이 사라졌다")
        res.close()
        # 받다 만 조각이 남아 있으면 안 된다
        leftovers = [n for n in os.listdir(store.program_dir())
                     if n.endswith(".part")]
        self.assertEqual(leftovers, [], "받다 만 조각이 남았다")

    def test_filename_cannot_escape_or_inject(self):
        """
        올리는 사람이 준 이름을 **저장 경로에 쓰지 않는다.**

        이름은 보여 주기와 내려받기 헤더에만 쓴다. 경로 계산에 쓰면 그 한 줄이
        파일시스템을 연다. 헤더에 줄바꿈이 들어가면 헤더를 하나 더 붙일 수 있다.
        """
        self.login("admin", "admin-pw-12345")
        here = os.path.abspath(store.program_dir())
        for name in ("..%s..%sescaped.exe" % (os.sep, os.sep),
                     "../../../../etc/passwd.exe",
                     "relay" + chr(0) + ".exe",
                     "A" * 400 + ".exe",
                     "한글 이름.exe"):
            res = self.upload(name=name)
            self.assertEqual(res.status_code, 200, "%r : %s"
                             % (name, res.get_data(as_text=True)))
            meta = store.program_info()
            self.assertEqual(meta["stored"], "relay.exe",
                             "저장 이름이 올린 이름에 끌려갔다: %r" % name)
            full = os.path.abspath(meta["path"])
            self.assertTrue(full.startswith(here + os.sep),
                            "프로그램 폴더를 벗어났다: %r" % full)

            res = self.c.get("/servers/program")
            self.assertEqual(res.status_code, 200)
            headers = "".join("%s:%s" % (k, v) for k, v in res.headers.items())
            self.assertNotIn(chr(13), headers)
            self.assertNotIn(chr(10), headers)
            res.close()


# ---------------------------------------------------------------------------
# 1.7 DB 파일을 통째로 훑어 비밀이 남았는지 본다
# ---------------------------------------------------------------------------
class TestNothingSecretOnDisk(Base):
    """
    표 하나하나를 확인하는 시험은 **새로 생긴 칸을 놓친다.**

    칼럼을 더하거나 audit 의 details 에 한 줄 덧붙일 때, 그 자리에 비밀이
    섞여도 기존 시험은 전부 초록이다. 그래서 여기서는 현실적인 흐름을 한 번
    돌린 다음 **DB 파일의 바이트를 직접** 훑는다. 어느 표, 어느 칼럼, 어느
    인덱스, WAL 까지 전부 포함된다.

    쓰는 미끼는 네 가지다.
        서버 비밀번호 / 저장 전 초안 비밀번호 / 명령 출력 / 비밀번호 프롬프트 뒤에 친 줄
    """

    PW = "ProbeServerPw-AAA111"
    DRAFT = "ProbeDraftPw-BBB222"
    OUTPUT = "ProbeOutput-CCC333"
    TYPED = "ProbeTypedSecret-DDD444"

    def setUp(self):
        super(TestNothingSecretOnDisk, self).setUp()
        self.login("admin", "admin-pw-12345")
        self._saved_provider = app_module.get_provider

    def tearDown(self):
        app_module.get_provider = self._saved_provider
        super(TestNothingSecretOnDisk, self).tearDown()

    def test_no_probe_survives_in_the_database_file(self):
        key = self.start_relay(outputs={"cat /etc/motd": self.OUTPUT + "\n"})

        # 1) 저장되는 비밀번호
        srv = self.make_server(name="probe-host", host="10.9.0.1",
                               auth_kind="password", key_name="", password=self.PW)

        # 2) 저장하지 않는 초안 비밀번호
        code, _ = self.j("POST", "/api/servers/test", {
            "name": "probe-draft", "host": "10.9.0.2", "username": "ops",
            "auth_kind": "password", "password": self.DRAFT})
        self.assertEqual(code, 200)

        # 3) 명령 출력 (챗봇이 고르고 돌린 길 그대로)
        fake = FakeProvider([FENCE % "cat /etc/motd", "확인했습니다."])
        app_module.get_provider = lambda db: fake
        code, data = self.j("POST", "/api/sessions",
                            {"name": "미끼", "visibility": "private",
                             "server_id": srv["id"]})
        self.assertEqual(code, 201, data)
        sid = data["session"]["id"]
        code, data = self.j("POST", "/api/sessions/%d/messages" % sid,
                            {"message": "motd 좀 보여줘"})
        self.assertEqual(code, 200, data)
        self.assertIn("cat /etc/motd", self.relay.ran)
        self.assertIn(self.OUTPUT, fake.asked[-1], "출력이 Claude 에게 가긴 했다")

        # 4) 비밀번호 프롬프트 뒤에 친 줄
        code, data = self.j("POST", "/api/term", {"server_id": srv["id"]})
        self.assertEqual(code, 201, data)
        term_id = data["term_id"]
        self.wait_for_term(term_id, "$")
        # 중계가 비밀번호를 물어보는 상황을 만든다
        self.c.post("/api/relay/result",
                    json={"jobs": [], "term": [{"term_id": term_id,
                                                "data": "\r\n[sudo] password: "}]},
                    headers={"X-Relay-Key": key})
        self.wait_for_term(term_id, "password:")
        code, _ = self.j("POST", "/api/term/%s/io" % term_id,
                         {"data": self.TYPED + "\r"})
        self.assertEqual(code, 200)
        time.sleep(0.4)
        self.j("POST", "/api/term/%s/close" % term_id)

        # 친 줄이 기록은 되되 내용은 가려졌는지 함께 본다
        c = conn()
        try:
            lines = [r["line"] for r in c.execute(
                "SELECT line FROM term_inputs WHERE term_id = ?", (term_id,))]
        finally:
            c.close()
        self.assertIn("(가려짐)", lines, "비밀번호 줄이 기록에서 가려지지 않았다")

        # ---- 이제 파일 바이트를 훑는다 -------------------------------------
        blob = self.database_bytes()
        for name, probe in (("서버 비밀번호", self.PW),
                            ("저장 전 초안 비밀번호", self.DRAFT),
                            ("명령 출력", self.OUTPUT),
                            ("비밀번호 프롬프트 뒤에 친 줄", self.TYPED)):
            self.assertNotIn(probe.encode("utf-8"), blob,
                             "%s 가 DB 파일에 평문으로 남았다 (%s)" % (name, probe))
            self.assertNotIn(probe.encode("utf-16-le"), blob,
                             "%s 가 DB 파일에 UTF-16 으로 남았다" % name)

        # 미끼가 실제로 흘렀다는 것도 확인한다. 아무것도 안 하고 통과하면 안 된다.
        self.assertTrue(srv["has_secret"], "비밀번호가 저장되긴 해야 한다")
        self.assertIn(b"probe-host", blob, "미끼 검사가 엉뚱한 파일을 본 것 같다")

    def wait_for_term(self, term_id, needle, timeout=6):
        seen, seq, end = "", 0, time.time() + timeout
        while time.time() < end:
            code, data = self.j("GET", "/api/term/%s/io?seq=%d" % (term_id, seq))
            seen += data.get("data") or ""
            seq = data.get("seq", seq)
            if needle in seen:
                return seen
            time.sleep(0.1)
        self.fail("터미널에서 %r 를 못 봤다. 받은 것: %r" % (needle, seen))

    @staticmethod
    def database_bytes():
        """본 파일 + WAL + 인덱스까지 전부. 먼저 WAL 을 본 파일로 밀어 넣는다."""
        c = conn()
        try:
            c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            c.close()
        out = b""
        for suffix in ("", "-wal", "-shm"):
            path = db_module.DATABASE_PATH + suffix
            if os.path.exists(path):
                with open(path, "rb") as f:
                    out += f.read()
        return out


# ---------------------------------------------------------------------------
# 2. 서버 등록과 연결 테스트
# ---------------------------------------------------------------------------
class TestServers(Base):
    def test_test_before_save(self):
        self.login("admin", "admin-pw-12345")
        self.start_relay()
        code, data = self.j("POST", "/api/servers/test",
                            {"name": "t1", "host": "10.0.0.9", "port": 22,
                             "username": "svc_ops", "auth_kind": "key",
                             "key_name": "ops-vdi-01"})
        self.assertEqual(code, 200, data)
        self.assertTrue(data["ok"])
        self.assertIn("OpenSSH", data["banner"])

    def test_test_without_relay_is_503(self):
        self.login("admin", "admin-pw-12345")
        code, data = self.j("POST", "/api/servers/test",
                            {"name": "t2", "host": "10.0.0.9", "port": 22,
                             "username": "svc_ops", "auth_kind": "key",
                             "key_name": "k"})
        self.assertEqual(code, 503)
        self.assertIn("중계", data.get("error", ""))

    def test_password_is_never_returned(self):
        self.login("admin", "admin-pw-12345")
        self.start_relay()
        srv = self.make_server(name="lto-arc-01", host="10.20.40.11",
                               auth_kind="password", key_name="",
                               password="SuperSecret!23")
        self.assertNotIn("secret_enc", srv)
        self.assertTrue(srv["has_secret"])
        self.assertNotIn("SuperSecret", json.dumps(srv, ensure_ascii=False))

        code, data = self.j("GET", "/api/servers")
        self.assertEqual(code, 200)
        self.assertNotIn("SuperSecret", json.dumps(data, ensure_ascii=False))

    def test_password_is_encrypted_at_rest(self):
        """
        조건을 달지 않는다.

        전에는 `if settings_store.encryption_available():` 로 감싸 두었다.
        그래서 cryptography 가 없는 개발 환경에서는 시험이 전부 초록인데도
        **암호화를 한 번도 검사하지 않았다.** 평문으로 저장되던 것을 시험이
        그냥 지나쳤다. requirements.txt 가 cryptography 를 요구하므로, 없으면
        시험이 깨지는 쪽이 맞다.
        """
        self.assertTrue(settings_store.encryption_available(),
                        "cryptography 가 없다. requirements.txt 에 들어 있으므로 "
                        "설치하고 다시 돌려라. (pip install cryptography)")
        self.login("admin", "admin-pw-12345")
        self.start_relay()
        self.make_server(name="pw-host", host="10.20.40.12", auth_kind="password",
                         key_name="", password="PlainTextProbe!")
        c = conn()
        try:
            raw = c.execute("SELECT secret_enc FROM ssh_servers WHERE name='pw-host'"
                            ).fetchone()["secret_enc"]
        finally:
            c.close()
        self.assertTrue(raw.startswith("enc:v1:"), raw[:40])
        self.assertNotIn("PlainTextProbe", raw)
        self.assertEqual(settings_store.decrypt_secret(raw), "PlainTextProbe!")

    def test_password_is_refused_when_it_cannot_be_encrypted(self):
        """
        암호화할 수 없으면 **저장하지 않는다.**

        서버 추가 화면에는 "접속 정보는 암호화해서 저장한다" 고 적혀 있다.
        cryptography 가 없을 때 조용히 평문으로 넣으면 그 말이 거짓이 된다.
        다른 서버로 들어가는 열쇠라서 DB 파일 하나가 새면 그 서버까지 넘어간다.
        막아도 키 인증은 그대로 쓸 수 있으니 길이 막히지 않는다.
        """
        self.login("admin", "admin-pw-12345")
        real = settings_store.encryption_available
        settings_store.encryption_available = lambda: False
        try:
            code, data = self.j("POST", "/api/servers", {
                "name": "no-enc", "host": "10.20.40.13", "username": "ops",
                "auth_kind": "password", "password": "ShouldNotBeStored!"})
            self.assertEqual(code, 409, data)
            self.assertIn("cryptography", data.get("error", ""))

            # 키 인증은 막히지 않는다. 열쇠는 중계 PC 에 있고 DB 에는 이름만 남는다.
            code, data = self.j("POST", "/api/servers", {
                "name": "key-ok", "host": "10.20.40.14", "username": "ops",
                "auth_kind": "key", "key_name": "id_rsa"})
            self.assertIn(code, (200, 201), data)
        finally:
            settings_store.encryption_available = real

        c = conn()
        try:
            rows = list(c.execute("SELECT name, secret_enc FROM ssh_servers"))
        finally:
            c.close()
        self.assertNotIn("no-enc", [r["name"] for r in rows], "거부했는데 들어갔다")
        blob = " ".join(r["secret_enc"] or "" for r in rows)
        self.assertNotIn("ShouldNotBeStored", blob)

    def test_admin_screen_counts_leftover_plaintext(self):
        """
        고치기 전에 들어간 평문은 남는다. 그래서 몇 건인지 알려 준다.

        새로 저장할 때만 막으면, 이미 들어간 값은 아무도 모르는 채로 평문으로
        남는다. 운영 화면이 세어서 "다시 저장하라" 고 말해 줘야 한다.
        """
        self.login("admin", "admin-pw-12345")
        self.make_server(name="legacy", host="10.20.40.15", auth_kind="password",
                         key_name="", password="Encrypted!")
        code, data = self.j("GET", "/api/admin/relay/summary")
        self.assertEqual(data.get("plaintext_secrets"), 0, data)

        # cryptography 없이 돌던 서버가 남긴 모양을 그대로 만든다
        c = conn()
        try:
            c.execute("UPDATE ssh_servers SET secret_enc = 'OldPlainText!'"
                      " WHERE name = 'legacy'")
            c.commit()
        finally:
            c.close()
        code, data = self.j("GET", "/api/admin/relay/summary")
        self.assertEqual(data.get("plaintext_secrets"), 1, data)

    def test_draft_password_never_touches_the_queue(self):
        """
        저장 전 「연결 테스트」의 비밀번호는 DB 를 거치지 않는다.

        초안은 아직 DB 에 없는 서버다. 그 비밀번호를 relay_jobs.payload 에
        적으면 "어느 표에도 비밀을 넣지 않는다" 가 그 자리에서 깨진다.
        """
        self.login("admin", "admin-pw-12345")
        self.start_relay()
        code, data = self.j("POST", "/api/servers/test",
                            {"name": "draft-pw", "host": "10.20.40.21", "port": 22,
                             "username": "svc_ops", "auth_kind": "password",
                             "key_name": "", "password": "DraftProbe!77"})
        self.assertEqual(code, 200, data)
        self.assertTrue(data["ok"])

        # 중계는 받았다
        drafts = [a for a in self.relay.auth_seen] + [
            p.get("draft") for p in self.relay.payloads if p.get("draft")]
        self.assertTrue(any("DraftProbe!77" == (d or {}).get("password")
                            for d in drafts),
                        "중계가 비밀번호를 받지 못했다: %r" % drafts)

        # DB 에는 없다
        c = conn()
        try:
            blob = " ".join(
                (r["payload"] or "") + (r["result"] or "")
                for r in c.execute("SELECT payload, result FROM relay_jobs"))
        finally:
            c.close()
        self.assertNotIn("DraftProbe", blob)

    def test_saved_password_is_sent_only_with_the_job(self):
        """저장된 서버의 비밀번호는 일을 받아 가는 그 응답에만 실린다."""
        self.login("admin", "admin-pw-12345")
        self.start_relay()
        srv = self.make_server(name="pw-host2", host="10.20.40.13",
                               auth_kind="password", key_name="",
                               password="QueueProbe!99")
        code, data = self.j("POST", "/api/servers/%d/test" % srv["id"])
        self.assertEqual(code, 200, data)
        self.assertTrue(any(a.get("password") == "QueueProbe!99"
                            for a in self.relay.auth_seen))
        c = conn()
        try:
            blob = " ".join(
                (r["payload"] or "") + (r["result"] or "")
                for r in c.execute("SELECT payload, result FROM relay_jobs"))
        finally:
            c.close()
        self.assertNotIn("QueueProbe", blob)

    def test_only_admin_can_add(self):
        self.login("admin", "admin-pw-12345")
        self.start_relay()
        srv = self.make_server(name="only-admin", host="10.20.30.190")
        self.grant("hong", level="read", all_servers=True)

        self.login("hong", "hong-pw-12345")
        code, _ = self.j("POST", "/api/servers",
                         {"name": "x", "host": "1.1.1.1", "username": "u",
                          "auth_kind": "key", "key_name": "k"})
        self.assertEqual(code, 403)
        code, _ = self.j("PATCH", "/api/servers/%d" % srv["id"], {"name": "y"})
        self.assertEqual(code, 403)
        code, _ = self.j("DELETE", "/api/servers/%d" % srv["id"])
        self.assertEqual(code, 403)


# ---------------------------------------------------------------------------
# 3. 사용 허용
# ---------------------------------------------------------------------------
class TestGrants(Base):
    def test_no_menu_no_api(self):
        """「서버」 메뉴가 없으면 주소를 직접 쳐도 막힌다."""
        self.login("hong", "hong-pw-12345")
        for method, url in (("GET", "/api/servers"), ("GET", "/api/term"),
                            ("POST", "/api/term")):
            code, _ = self.j(method, url, {} if method == "POST" else None)
            self.assertEqual(code, 403, "%s %s" % (method, url))
        res = self.c.get("/servers")
        self.assertEqual(res.status_code, 403)

    def test_scope_filters_the_list(self):
        self.login("admin", "admin-pw-12345")
        self.start_relay()
        a = self.make_server(name="scope-a", host="10.1.0.1")
        b = self.make_server(name="scope-b", host="10.1.0.2")
        self.grant("kim", level="read", all_servers=False, server_ids=[a["id"]])

        self.login("kim", "kim-pw-12345")
        code, data = self.j("GET", "/api/servers")
        self.assertEqual(code, 200)
        names = [s["name"] for s in data["servers"]]
        self.assertIn("scope-a", names)
        self.assertNotIn("scope-b", names)

        # 범위 밖의 서버로는 터미널도 못 열고 대화에도 못 붙인다
        code, _ = self.j("POST", "/api/term", {"server_id": b["id"]})
        self.assertEqual(code, 403)
        code, _ = self.j("POST", "/api/sessions", {"server_id": b["id"]})
        self.assertEqual(code, 403)

    def test_turning_off_closes_terminals(self):
        self.login("admin", "admin-pw-12345")
        self.start_relay()
        srv = self.make_server(name="off-test", host="10.1.0.3")
        self.grant("kim", level="read", all_servers=True)

        self.login("kim", "kim-pw-12345")
        self.start_relay()                     # 김운영 자기 VDI 의 중계
        code, data = self.j("POST", "/api/term", {"server_id": srv["id"]})
        self.assertEqual(code, 201, data)
        term_id = data["term_id"]

        self.login("admin", "admin-pw-12345")
        code, data = self.j("PUT", "/api/admin/relay/grants/%d"
                            % auth.user_by_name(conn(), "kim")["id"],
                            {"level": "off", "all_servers": False, "server_ids": []})
        self.assertEqual(code, 200, data)
        self.assertGreaterEqual(data["terminals_closed"], 1)

        c = conn()
        try:
            row = c.execute("SELECT state FROM term_sessions WHERE id = ?",
                            (term_id,)).fetchone()
        finally:
            c.close()
        self.assertEqual(row["state"], "closed")

    def test_admin_row_is_locked(self):
        self.login("admin", "admin-pw-12345")
        uid = auth.user_by_name(conn(), "admin")["id"]
        code, data = self.j("PUT", "/api/admin/relay/grants/%d" % uid,
                            {"level": "off", "all_servers": False, "server_ids": []})
        self.assertEqual(code, 400)
        self.assertIn("관리자", data.get("error", ""))


# ---------------------------------------------------------------------------
# 4. 웹 터미널
# ---------------------------------------------------------------------------
class TestTerminal(Base):
    def setUp(self):
        super(TestTerminal, self).setUp()
        self.login("admin", "admin-pw-12345")
        self.start_relay()
        self.srv = self.make_server(name="term-host", host="10.2.0.1")

    def test_open_read_write_close(self):
        code, data = self.j("POST", "/api/term", {"server_id": self.srv["id"]})
        self.assertEqual(code, 201, data)
        term_id = data["term_id"]

        # 중계가 프롬프트를 올려 준다
        prompt = self.wait_for(term_id, "$")
        self.assertIn("svc_ops", prompt)

        # 사람이 치면 에코로 돌아온다
        code, _ = self.j("POST", "/api/term/%s/io" % term_id, {"data": "df -h\r"})
        self.assertEqual(code, 200)
        echoed = self.wait_for(term_id, "df -h", after=0)
        self.assertIn("df -h", echoed)

        # 친 줄이 기록에 남는다
        c = conn()
        try:
            lines = [r["line"] for r in c.execute(
                "SELECT line FROM term_inputs WHERE term_id = ?", (term_id,))]
        finally:
            c.close()
        self.assertIn("df -h", lines)

        code, _ = self.j("POST", "/api/term/%s/close" % term_id)
        self.assertEqual(code, 200)
        code, data = self.j("GET", "/api/term/%s/io?seq=0" % term_id)
        self.assertEqual(data["state"], "closed")

    def wait_for(self, term_id, needle, after=0, timeout=5):
        seen = ""
        seq = after
        end = time.time() + timeout
        while time.time() < end:
            code, data = self.j("GET", "/api/term/%s/io?seq=%d" % (term_id, seq))
            self.assertEqual(code, 200, data)
            seen += data.get("data") or ""
            seq = data.get("seq", seq)
            if needle in seen:
                return seen
            time.sleep(0.1)
        self.fail("터미널에서 %r 를 받지 못했습니다. 받은 것: %r" % (needle, seen))

    def test_other_people_cannot_read_my_terminal(self):
        code, data = self.j("POST", "/api/term", {"server_id": self.srv["id"]})
        term_id = data["term_id"]
        self.grant("hong", level="read", all_servers=True)

        self.login("hong", "hong-pw-12345")
        code, _ = self.j("GET", "/api/term/%s/io?seq=0" % term_id)
        self.assertEqual(code, 403, "남의 터미널 화면이 보이면 안 된다")
        code, _ = self.j("POST", "/api/term/%s/io" % term_id, {"data": "x"})
        self.assertEqual(code, 403)

    def test_limit_per_person(self):
        set_setting("relay_term_max_per_user", "2")
        opened = []
        for i in range(2):
            code, data = self.j("POST", "/api/term", {"server_id": self.srv["id"]})
            self.assertEqual(code, 201, data)
            opened.append(data["term_id"])
        code, data = self.j("POST", "/api/term", {"server_id": self.srv["id"]})
        self.assertEqual(code, 409)
        self.assertIn("2개", data.get("error", ""))
        for t in opened:
            self.j("POST", "/api/term/%s/close" % t)

    def test_paste_size_limit(self):
        code, data = self.j("POST", "/api/term", {"server_id": self.srv["id"]})
        term_id = data["term_id"]
        big = "a" * (app_module.config.RELAY_TERM_MAX_INPUT_BYTES + 10)
        code, data = self.j("POST", "/api/term/%s/io" % term_id, {"data": big})
        self.assertEqual(code, 413)
        self.j("POST", "/api/term/%s/close" % term_id)

    def test_password_prompt_line_is_masked(self):
        """비밀번호를 묻는 프롬프트 뒤에 친 줄은 내용을 적지 않는다."""
        code, data = self.j("POST", "/api/term", {"server_id": self.srv["id"]})
        term_id = data["term_id"]
        self.wait_for(term_id, "$")

        stream = store.HUB.get(term_id)
        stream.push("[sudo] password for svc_ops: ")
        self.j("POST", "/api/term/%s/io" % term_id, {"data": "hunter2\r"})

        c = conn()
        try:
            lines = [r["line"] for r in c.execute(
                "SELECT line FROM term_inputs WHERE term_id = ?", (term_id,))]
        finally:
            c.close()
        self.assertIn("(가려짐)", lines)
        self.assertNotIn("hunter2", " ".join(lines))
        self.j("POST", "/api/term/%s/close" % term_id)


# ---------------------------------------------------------------------------
# 5. 채팅에서 명령을 고르는 길
# ---------------------------------------------------------------------------
class TestChat(Base):
    def setUp(self):
        super(TestChat, self).setUp()
        self.login("admin", "admin-pw-12345")
        self.start_relay(outputs={
            "df -h /data": "Filesystem  Size  Used Avail Use%\n/dev/sdb1  18T  15T  2.6T  86%\n",
            "find /data/log -type f -mtime +90 -delete": "",
        })
        self.srv = self.make_server(name="chat-host", host="10.3.0.1")
        self._saved_provider = app_module.get_provider

    def tearDown(self):
        app_module.get_provider = self._saved_provider
        super(TestChat, self).tearDown()

    def use_provider(self, replies):
        fake = FakeProvider(replies)
        app_module.get_provider = lambda db: fake
        return fake

    def new_session(self, server_id=None, visibility="private"):
        body = {"name": "시험 대화", "visibility": visibility}
        if server_id is not None:
            body["server_id"] = server_id
        code, data = self.j("POST", "/api/sessions", body)
        self.assertEqual(code, 201, data)
        return data["session"]

    def ask(self, sid, text="물어본다"):
        return self.j("POST", "/api/sessions/%d/messages" % sid, {"message": text})

    # --- 조회 -------------------------------------------------------
    def test_read_command_runs_without_approval(self):
        sess = self.new_session(self.srv["id"])
        fake = self.use_provider([
            "확인해 보겠습니다.\n```ssh\ndf -h /data\n```",
            "2.6T 남았습니다. 86% 를 쓰고 있습니다.",
        ])
        code, data = self.ask(sess["id"], "181 /data 얼마나 남았어?")
        self.assertEqual(code, 200, data)
        self.assertTrue(data["ok"])
        self.assertIn("df -h /data", self.relay.ran)
        self.assertEqual(data["ssh"]["ran"], 1)
        self.assertEqual(data["ssh"]["pending"], 0)

        # 답에는 명령 블록이 남지 않고, 두 번째 왕복의 답이 본문이 된다
        last = data["messages"][-1]
        self.assertIn("2.6T", last["content"])
        self.assertNotIn("```ssh", last["content"])

        # 실행 결과가 Claude 에게 넘어갔다
        self.assertIn("86%", fake.asked[-1])

        # 기록에는 명령이 남고 출력은 남지 않는다
        c = conn()
        try:
            row = c.execute("SELECT * FROM ssh_commands ORDER BY id DESC LIMIT 1"
                            ).fetchone()
        finally:
            c.close()
        self.assertEqual(row["state"], "done")
        self.assertEqual(row["level"], "read")
        self.assertNotIn("86%", row["result_note"])
        self.assertIn("줄", row["result_note"])

    def test_command_output_never_lands_in_the_queue(self):
        """
        명령의 출력은 그 대화에만 남는다.

        큐에 적어 두면 90일 동안 남아서, private 대화의 내용을 대화 밖에서
        읽을 수 있게 된다. 관리자도 private 대화는 볼 수 없어야 한다.
        """
        sess = self.new_session(self.srv["id"])
        self.use_provider([FENCE % "df -h /data", "2.6T 남았습니다."])
        self.ask(sess["id"], "얼마나 남았어?")
        c = conn()
        try:
            blob = " ".join(
                (r["payload"] or "") + (r["result"] or "")
                for r in c.execute("SELECT payload, result FROM relay_jobs"))
            notes = " ".join(r["result_note"] for r in
                             c.execute("SELECT result_note FROM ssh_commands"))
        finally:
            c.close()
        self.assertNotIn("Filesystem", blob)
        self.assertNotIn("18T", blob)
        self.assertNotIn("Filesystem", notes)
        # 대화에는 남는다 (그 대화를 볼 수 있는 사람의 것이다)
        msgs = self.j("GET", "/api/sessions/%d/messages" % sess["id"])[1]["messages"]
        self.assertIn("2.6T", msgs[-1]["content"])

    def test_prompt_prefix_only_when_server_is_bound(self):
        sess = self.new_session(None)
        fake = self.use_provider(["서버가 없어 확인할 수 없습니다."])
        self.ask(sess["id"], "디스크 얼마나 남았어?")
        self.assertNotIn("[서버 작업 안내]", fake.asked[0])

        sess2 = self.new_session(self.srv["id"])
        fake2 = self.use_provider(["네."])
        self.ask(sess2["id"], "안녕")
        self.assertIn("[서버 작업 안내]", fake2.asked[0])
        self.assertIn("chat-host", fake2.asked[0])

    # --- 변경 -------------------------------------------------------
    def test_write_command_waits_for_approval(self):
        set_setting("relay_policy", "write")
        self.grant("admin", level="write") if False else None
        sess = self.new_session(self.srv["id"])
        self.use_provider([
            "90일 넘은 로그를 지우겠습니다.\n```ssh\nfind /data/log -type f -mtime +90 -delete\n```",
        ])
        code, data = self.ask(sess["id"], "90일 넘은 로그는 지워줘")
        self.assertEqual(code, 200, data)
        self.assertEqual(data["ssh"]["pending"], 1)
        self.assertNotIn("find /data/log", self.relay.ran,
                         "승인 전에 서버로 나가면 안 된다")

        msgs = self.j("GET", "/api/sessions/%d/messages" % sess["id"])[1]["messages"]
        cards = msgs[-1]["commands"]
        self.assertEqual(len(cards), 1)
        card = cards[0]
        self.assertEqual(card["state"], "pending")
        self.assertEqual(card["level"], "write")
        self.assertGreater(card["expires_in"], 0)

        # 승인하면 그때 나간다
        code, data = self.j("POST", "/api/ssh/commands/%d/approve" % card["id"])
        self.assertEqual(code, 200, data)
        self.assertIn("find /data/log -type f -mtime +90 -delete", self.relay.ran)
        self.assertEqual(data["command"]["state"], "done")
        self.assertEqual(data["command"]["approver_name"], "관리자")

        # 결과와 "누가 승인했는지" 가 같은 자리에 남는다
        msgs = self.j("GET", "/api/sessions/%d/messages" % sess["id"])[1]["messages"]
        self.assertIn("승인 후 실행", msgs[-1]["content"])
        self.assertEqual(msgs[-1]["commands"][0]["approver_name"], "관리자")
        set_setting("relay_policy", "read")

    def test_reject_does_not_run(self):
        set_setting("relay_policy", "write")
        sess = self.new_session(self.srv["id"])
        self.use_provider(["```ssh\nrm -f /data/x\n```"])
        self.ask(sess["id"], "지워줘")
        msgs = self.j("GET", "/api/sessions/%d/messages" % sess["id"])[1]["messages"]
        card = msgs[-1]["commands"][0]
        code, data = self.j("POST", "/api/ssh/commands/%d/reject" % card["id"])
        self.assertEqual(code, 200)
        self.assertEqual(data["command"]["state"], "rejected")
        self.assertNotIn("rm -f /data/x", self.relay.ran)

        # 거절한 것을 다시 승인할 수 없다
        code, data = self.j("POST", "/api/ssh/commands/%d/approve" % card["id"])
        self.assertEqual(code, 409)
        set_setting("relay_policy", "read")

    def test_expired_card_cannot_be_approved(self):
        set_setting("relay_policy", "write")
        set_setting("relay_approval_seconds", "30")
        sess = self.new_session(self.srv["id"])
        self.use_provider(["```ssh\nrm -f /data/y\n```"])
        self.ask(sess["id"], "지워줘")
        c = conn()
        try:
            row = c.execute("SELECT id FROM ssh_commands WHERE state='pending'"
                            " ORDER BY id DESC LIMIT 1").fetchone()
            # 시계를 돌리지 않고 만든 시각을 과거로 옮긴다
            c.execute("UPDATE ssh_commands SET created_at ="
                      " datetime('now','localtime','-10 minutes') WHERE id = ?",
                      (row["id"],))
            c.commit()
        finally:
            c.close()
        code, data = self.j("POST", "/api/ssh/commands/%d/approve" % row["id"])
        self.assertEqual(code, 409)
        self.assertIn("시간", data.get("error", ""))
        self.assertNotIn("rm -f /data/y", self.relay.ran)
        set_setting("relay_policy", "read")
        set_setting("relay_approval_seconds", "120")

    def test_policy_ceiling_blocks_write(self):
        """정책이 '조회만' 이면 변경 명령은 승인 카드조차 만들지 않는다."""
        set_setting("relay_policy", "read")
        sess = self.new_session(self.srv["id"])
        self.use_provider(["```ssh\nrm -f /data/z\n```"])
        code, data = self.ask(sess["id"], "지워줘")
        self.assertEqual(data["ssh"]["denied"], 1)
        self.assertEqual(data["ssh"]["pending"], 0)
        msgs = self.j("GET", "/api/sessions/%d/messages" % sess["id"])[1]["messages"]
        self.assertEqual(msgs[-1]["commands"][0]["state"], "denied")
        self.assertNotIn("rm -f /data/z", self.relay.ran)

    def test_blocked_command_never_leaves(self):
        set_setting("relay_policy", "write")
        sess = self.new_session(self.srv["id"])
        self.use_provider(["```ssh\ncat /etc/shadow\n```"])
        code, data = self.ask(sess["id"], "계정 좀 보여줘")
        self.assertEqual(data["ssh"]["blocked"], 1)
        msgs = self.j("GET", "/api/sessions/%d/messages" % sess["id"])[1]["messages"]
        card = msgs[-1]["commands"][0]
        self.assertEqual(card["state"], "blocked")
        self.assertNotIn("cat /etc/shadow", self.relay.ran)
        # 차단된 것은 승인 단추가 있어도 눌리지 않는다
        code, _ = self.j("POST", "/api/ssh/commands/%d/approve" % card["id"])
        self.assertEqual(code, 409)
        set_setting("relay_policy", "read")

    def test_max_commands_per_question(self):
        set_setting("relay_chat_max_commands", "2")
        sess = self.new_session(self.srv["id"])
        self.use_provider([
            "```ssh\ndf -h /data\n```\n```ssh\ndf -h /data\n```\n```ssh\ndf -h /data\n```",
            "끝났습니다.",
        ])
        code, data = self.ask(sess["id"], "세 번 봐줘")
        self.assertEqual(data["ssh"]["ran"], 2)
        set_setting("relay_chat_max_commands", "3")

    # --- 서버 붙이기 / 바꾸기 ---------------------------------------
    def test_server_bound_session_cannot_be_public(self):
        code, data = self.j("POST", "/api/sessions",
                            {"name": "공개", "visibility": "public",
                             "server_id": self.srv["id"]})
        self.assertEqual(code, 400)
        self.assertIn("공개", data.get("error", ""))

        sess = self.new_session(self.srv["id"])
        code, data = self.j("PATCH", "/api/sessions/%d" % sess["id"],
                            {"visibility": "public"})
        self.assertEqual(code, 400)

    def test_change_server_in_session(self):
        other = self.make_server(name="chat-host-2", host="10.3.0.2")
        sess = self.new_session(self.srv["id"])
        code, data = self.j("PATCH", "/api/sessions/%d" % sess["id"],
                            {"server_id": other["id"]})
        self.assertEqual(code, 200)
        self.assertEqual(data["session"]["server"]["name"], "chat-host-2")

        code, data = self.j("PATCH", "/api/sessions/%d" % sess["id"],
                            {"server_id": None})
        self.assertEqual(code, 200)
        self.assertIsNone(data["session"]["server"])

    def test_question_is_refused_when_server_is_disabled(self):
        sess = self.new_session(self.srv["id"])
        self.j("PATCH", "/api/servers/%d" % self.srv["id"], {"is_enabled": False})
        self.use_provider(["아무 말"])
        code, data = self.ask(sess["id"], "확인해줘")
        self.assertEqual(code, 409)
        self.assertIn("꺼져", data.get("error", ""))
        self.j("PATCH", "/api/servers/%d" % self.srv["id"], {"is_enabled": True})

    def test_question_is_refused_when_grant_is_gone(self):
        """화면에서 고른 것을 믿지 않는다. 질문마다 다시 본다."""
        self.grant("kim", level="read", all_servers=True)
        self.login("kim", "kim-pw-12345")
        sess = self.new_session(self.srv["id"])

        self.login("admin", "admin-pw-12345")
        self.grant("kim", level="off", all_servers=False, server_ids=[])

        self.login("kim", "kim-pw-12345")
        self.use_provider(["아무 말"])
        code, data = self.ask(sess["id"], "확인해줘")
        self.assertEqual(code, 403)

    def test_server_sessions_list(self):
        sess = self.new_session(self.srv["id"])
        code, data = self.j("GET", "/api/servers/%d/sessions" % self.srv["id"])
        self.assertEqual(code, 200)
        self.assertIn(sess["id"], [x["id"] for x in data["same"]])


# ---------------------------------------------------------------------------
# 6. 긴 대기가 다른 요청을 막지 않는가
# ---------------------------------------------------------------------------
class TestConcurrency(Base):
    """
    중계는 25초짜리 긴 대기를 쓴다. 그 대기가 DB 쓰기 락을 들고 있으면 그 동안
    서버 전체의 쓰기가 막힌다. busy_timeout(15초)을 넘기면 "database is locked"
    로 500 이 난다. 실제로 그렇게 났고, 그래서 이 시험이 있다.

    take_jobs 가 "가져간 것이 없을 때" 커밋하지 않는 것이 원인이었다. UPDATE 가
    0행을 고쳐도 쓰기 트랜잭션은 이미 열려 있다.
    """

    def test_write_is_not_blocked_during_a_long_poll(self):
        set_setting("relay_poll_seconds", "20")
        try:
            self.login("admin", "admin-pw-12345")
            self.start_relay()
            # 중계가 긴 대기에 들어갈 시간을 준다
            time.sleep(1.0)
            for i in range(3):
                t0 = time.time()
                code, data = self.j("POST", "/api/my-relay/enroll", {})
                took = time.time() - t0
                self.assertEqual(code, 200, data)
                self.assertLess(took, 5.0,
                                "긴 대기 중에 쓰기가 %.1f초 걸렸다. 쓰기 락을 "
                                "들고 기다리고 있다." % took)
        finally:
            set_setting("relay_poll_seconds", "1")

    def test_two_relays_racing_for_one_job_do_not_deadlock(self):
        """
        중계가 두 대 붙어 있으면 같은 일을 두고 경쟁한다. 진 쪽의 UPDATE 는
        0행이고, 그때 커밋을 빼먹으면 그 연결이 락을 들고 기다린다.
        """
        set_setting("relay_poll_seconds", "10")
        try:
            self.login("admin", "admin-pw-12345")
            key = self.start_relay()
            second = FakeRelay(key)       # 같은 토큰으로 한 대 더
            second.start()
            second.ready.wait(3)
            self.relays.append(second)
            time.sleep(0.5)
            try:
                for _ in range(3):
                    t0 = time.time()
                    code, data = self.j("POST", "/api/servers/test",
                                        {"name": "race", "host": "10.9.9.9",
                                         "port": 22, "username": "u",
                                         "auth_kind": "key", "key_name": "k"})
                    self.assertEqual(code, 200, data)
                    self.assertTrue(data["ok"], data)
                    self.assertLess(time.time() - t0, 8.0)
            finally:
                pass
        finally:
            set_setting("relay_poll_seconds", "1")


# ---------------------------------------------------------------------------
# 7. 기록
# ---------------------------------------------------------------------------
class TestLog(Base):
    def test_log_has_three_sources_and_no_delete(self):
        self.login("admin", "admin-pw-12345")
        self.start_relay(outputs={"df -h": "ok\n"})
        srv = self.make_server(name="log-host", host="10.4.0.1")

        # 1) 채팅 명령
        fake = FakeProvider(["```ssh\ndf -h\n```", "끝"])
        app_module.get_provider = lambda db: fake
        code, data = self.j("POST", "/api/sessions",
                            {"name": "기록 시험", "server_id": srv["id"]})
        sid = data["session"]["id"]
        self.j("POST", "/api/sessions/%d/messages" % sid, {"message": "봐줘"})
        app_module.get_provider = app_module.get_provider

        # 2) 터미널
        code, data = self.j("POST", "/api/term", {"server_id": srv["id"]})
        term_id = data["term_id"]
        self.j("POST", "/api/term/%s/io" % term_id, {"data": "uptime\r"})
        self.j("POST", "/api/term/%s/close" % term_id)

        code, data = self.j("GET", "/api/admin/relay/log?days=1")
        self.assertEqual(code, 200)
        wheres = {r["where"] for r in data["rows"]}
        self.assertIn("채팅", wheres)
        self.assertIn("터미널", wheres)
        self.assertIn("설정", wheres)

        # 명령문은 남고 출력은 남지 않는다
        blob = json.dumps(data, ensure_ascii=False)
        self.assertIn("df -h", blob)
        self.assertNotIn("Filesystem", blob)

        # 터미널 한 줄 펼쳐 보기
        code, lines = self.j("GET", "/api/admin/relay/log/term/%s" % term_id)
        self.assertEqual(code, 200)
        self.assertIn("uptime", " ".join(x["line"] for x in lines["lines"]))

        # 지우는 길이 없다
        for method in ("DELETE", "POST"):
            res = getattr(self.c, method.lower())(
                "/api/admin/relay/log",
                headers={"X-CSRF-Token": self.csrf})
            self.assertIn(res.status_code, (404, 405),
                          "기록을 지우거나 바꾸는 길이 열려 있다")

    def test_csv_export(self):
        self.login("admin", "admin-pw-12345")
        res = self.c.get("/api/admin/relay/log.csv?days=7",
                         headers={"X-CSRF-Token": getattr(self, "csrf", "")})
        self.assertEqual(res.status_code, 200)
        self.assertIn("text/csv", res.headers["Content-Type"])
        body = res.get_data(as_text=True)
        self.assertTrue(body.startswith("﻿"), "엑셀용 BOM 이 있어야 한다")
        self.assertIn("시각", body)

    def test_normal_user_cannot_read_log(self):
        self.login("hong", "hong-pw-12345")
        code, _ = self.j("GET", "/api/admin/relay/log")
        self.assertEqual(code, 403)
        res = self.c.get("/admin/relay/log")
        self.assertEqual(res.status_code, 403)


# ---------------------------------------------------------------------------
# 8. 화면이 열리는지
# ---------------------------------------------------------------------------
class TestPages(Base):
    def test_pages_render(self):
        self.login("admin", "admin-pw-12345")
        self.start_relay()
        srv = self.make_server(name="page-host", host="10.5.0.1")
        for url in ("/servers", "/servers/%d/terminal" % srv["id"],
                    "/admin/relay", "/admin/relay/grants", "/admin/relay/log"):
            res = self.c.get(url)
            self.assertEqual(res.status_code, 200, url)
            self.assertIn("text/html", res.headers["Content-Type"])

    def test_rail_shows_server_menu(self):
        self.login("admin", "admin-pw-12345")
        html = self.c.get("/servers").get_data(as_text=True)
        self.assertIn('href="/servers"', html)
        self.assertIn("중계 설정", html)

    def test_chat_page_has_no_secret(self):
        self.login("admin", "admin-pw-12345")
        html = self.c.get("/").get_data(as_text=True)
        self.assertNotIn("secret_enc", html)


if __name__ == "__main__":
    unittest.main(verbosity=2)
