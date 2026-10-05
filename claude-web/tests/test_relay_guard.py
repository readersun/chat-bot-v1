#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
막는 자리와 청소하는 자리 시험.

세 가지를 본다.

    1. 새로 생긴 주소를 **로그인하지 않고** 또는 **권한 없이** 두드렸을 때
       전부 막히는지. 화면에서 숨기는 것은 막은 것이 아니다.
    2. 마이그레이션이 기존 DB 를 깨지 않는지. (v3 / v7 에서 올라오는 두 길)
    3. 청소 스레드가 할 일을 하는지. (승인 시간 초과, 조용한 터미널,
       중계 없이 쌓인 큐)

    python -m unittest tests.test_relay_guard
"""

import atexit
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_TMP = tempfile.mkdtemp(prefix="relay-guard-")
os.environ["DATABASE_PATH"] = os.path.join(_TMP, "chat.db")
os.environ["UPLOAD_DIR"] = os.path.join(_TMP, "uploads")
os.environ["NOTES_DIR"] = os.path.join(_TMP, "notes")
os.environ["BACKUP_DIR"] = os.path.join(_TMP, "backups")
os.environ["SECRET_KEY"] = "test-secret-key-for-relay-guard"
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

APP = app_module.app


def conn():
    return db_module.connect()


# ---------------------------------------------------------------------------
# 1. 막는 자리
# ---------------------------------------------------------------------------
# (method, url, 로그인 안 함, 권한 없는 사용자, 관리자 아닌 사용자)
#
# 기대값은 "막혀야 한다" 는 뜻의 집합이다. 200 이 들어 있으면 그 줄은 시험이
# 아니라 장식이므로 절대 넣지 않는다.
BLOCKED = (401, 403, 404, 302, 308)

# 토큰 없이 상태를 바꾸려 하면 CSRF 검사가 먼저 막는다(400). 그것도 막힌 것이다.
BLOCKED_OR_CSRF = BLOCKED + (400,)

USER_ROUTES = [
    ("GET", "/servers"),
    ("GET", "/servers/1/terminal"),
    ("GET", "/api/servers"),
    ("POST", "/api/servers"),
    ("POST", "/api/servers/test"),
    ("POST", "/api/servers/1/test"),
    ("PATCH", "/api/servers/1"),
    ("DELETE", "/api/servers/1"),
    ("GET", "/api/servers/1/sessions"),
    ("GET", "/api/term"),
    ("POST", "/api/term"),
    ("GET", "/api/term/abc/io"),
    ("POST", "/api/term/abc/io"),
    ("POST", "/api/term/abc/close"),
    ("POST", "/api/ssh/commands/1/approve"),
    ("POST", "/api/ssh/commands/1/reject"),
    ("GET", "/api/my-relay"),
    ("POST", "/api/my-relay/enroll"),
    ("POST", "/api/my-relay/revoke"),
    ("GET", "/servers/program"),
]

ADMIN_ROUTES = [
    ("GET", "/admin/relay"),
    ("GET", "/admin/relay/grants"),
    ("GET", "/admin/relay/log"),
    ("GET", "/api/admin/relay/summary"),
    ("POST", "/api/admin/relay/program"),
    ("DELETE", "/api/admin/relay/program"),
    ("POST", "/api/admin/relay/revoke"),
    ("POST", "/api/admin/relay/settings"),
    ("GET", "/api/admin/relay/grants"),
    ("PUT", "/api/admin/relay/grants/1"),
    ("GET", "/api/admin/relay/log"),
    ("GET", "/api/admin/relay/log.csv"),
    ("GET", "/api/admin/relay/log/term/abc"),
]

RELAY_ROUTES = [
    ("POST", "/api/relay/poll"),
    ("POST", "/api/relay/result"),
    ("POST", "/api/relay/beat"),
]


class TestGuards(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        c = conn()
        try:
            if not auth.user_by_name(c, "admin"):
                auth.create_user(c, "admin", "admin-pw-12345", "관리자", role="admin")
            if not auth.user_by_name(c, "plain"):
                uid = auth.create_user(c, "plain", "plain-pw-12345", "일반")
                permissions.set_user_menus(c, uid, ["chat"], None)
            c.commit()
        finally:
            c.close()

    def login(self, username, password):
        c = APP.test_client()
        page = c.get("/login").get_data(as_text=True)
        tok = re.search(r'name="_csrf" value="([^"]+)"', page).group(1)
        c.post("/login", data={"username": username, "password": password,
                               "_csrf": tok})
        csrf = c.get("/api/me").get_json()["csrf_token"]
        return c, csrf

    def hit(self, client, method, url, csrf=""):
        return getattr(client, method.lower())(
            url, headers={"X-CSRF-Token": csrf},
            json={} if method in ("POST", "PUT", "PATCH") else None)

    def anon(self):
        """
        로그인하지 않은 브라우저. CSRF 토큰은 가지고 있다.

        토큰 없이 POST 하면 CSRF 검사에서 400 으로 먼저 막힌다. 그러면 "권한
        검사가 있는지" 를 확인하지 못한다. 그래서 토큰은 제대로 받아 두고
        권한만 없는 상태를 만든다.
        """
        c = APP.test_client()
        page = c.get("/login").get_data(as_text=True)
        tok = re.search(r'name="_csrf" value="([^"]+)"', page).group(1)
        return c, tok

    def test_anonymous_is_blocked_everywhere(self):
        c, csrf = self.anon()
        for method, url in USER_ROUTES + ADMIN_ROUTES:
            with self.subTest(url="%s %s" % (method, url)):
                res = self.hit(c, method, url, csrf)
                self.assertIn(res.status_code, BLOCKED,
                              "%s %s 가 %d 로 열려 있다" % (method, url,
                                                          res.status_code))

    def test_anonymous_without_csrf_is_blocked_too(self):
        c = APP.test_client()
        for method, url in USER_ROUTES + ADMIN_ROUTES:
            with self.subTest(url="%s %s" % (method, url)):
                res = self.hit(c, method, url)
                self.assertIn(res.status_code, BLOCKED_OR_CSRF,
                              "%s %s 가 %d 로 열려 있다" % (method, url,
                                                          res.status_code))

    def test_user_without_server_menu_is_blocked(self):
        """채팅만 받은 사람은 서버 쪽 주소를 직접 쳐도 들어갈 수 없다."""
        c, csrf = self.login("plain", "plain-pw-12345")
        for method, url in USER_ROUTES:
            with self.subTest(url="%s %s" % (method, url)):
                res = self.hit(c, method, url, csrf)
                self.assertIn(res.status_code, BLOCKED,
                              "%s %s 가 %d 로 열려 있다" % (method, url,
                                                          res.status_code))

    def test_user_cannot_reach_admin_routes(self):
        c, csrf = self.login("plain", "plain-pw-12345")
        for method, url in ADMIN_ROUTES:
            with self.subTest(url="%s %s" % (method, url)):
                res = self.hit(c, method, url, csrf)
                self.assertIn(res.status_code, BLOCKED,
                              "%s %s 가 %d 로 열려 있다" % (method, url,
                                                          res.status_code))

    def test_relay_routes_need_the_key(self):
        c = APP.test_client()
        for method, url in RELAY_ROUTES:
            with self.subTest(url=url):
                res = self.hit(c, method, url)
                self.assertEqual(res.status_code, 401, url)

    def test_csrf_is_required_for_state_changes(self):
        """토큰 없이 POST 하면 400 이다. (중계 API 만 예외)"""
        c, _csrf = self.login("admin", "admin-pw-12345")
        res = c.post("/api/my-relay/enroll", json={})
        self.assertEqual(res.status_code, 400)

    def test_login_page_is_reachable(self):
        """막는 시험만 하다가 로그인 자체가 막히면 아무도 못 들어온다."""
        res = APP.test_client().get("/login")
        self.assertEqual(res.status_code, 200)


# ---------------------------------------------------------------------------
# 2. 마이그레이션
# ---------------------------------------------------------------------------
V1_SCHEMA = """
CREATE TABLE projects (
    id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL);
CREATE TABLE sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT, project_id INTEGER NOT NULL,
    name TEXT NOT NULL, claude_session_id TEXT, created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL);
CREATE TABLE messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT, session_id INTEGER NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('user','assistant','error')),
    content TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE attachments (
    id INTEGER PRIMARY KEY AUTOINCREMENT, session_id INTEGER NOT NULL,
    message_id INTEGER, original_name TEXT NOT NULL, stored_name TEXT NOT NULL,
    file_path TEXT NOT NULL, mime_type TEXT NOT NULL, file_size INTEGER NOT NULL,
    created_at TEXT NOT NULL);
"""


LEGACY_RELAY = """
-- 운영 서버에 남아 있던 예전 설계. 이름만 같고 칸이 전부 다르다.
CREATE TABLE relay_agents (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT NOT NULL UNIQUE,
    label        TEXT NOT NULL DEFAULT '',
    token_hash   TEXT NOT NULL,
    token_prefix TEXT NOT NULL DEFAULT '',
    hosts_json   TEXT NOT NULL DEFAULT '[]',
    client_version TEXT NOT NULL DEFAULT '',
    is_active    INTEGER NOT NULL DEFAULT 1,
    last_seen_at TEXT,
    created_at   TEXT NOT NULL,
    created_by   INTEGER
);
CREATE INDEX idx_relay_agents_owner ON relay_agents(is_active);
"""


class TestMigration(unittest.TestCase):
    """
    기존 DB 를 깨지 않는지 본다.

    두 길을 본다.
      v1 (로그인도 없던 시절) -> v8
      v7 (서버 기능 직전)     -> v8   : user_menus 의 CHECK 제약을 다시 만든다
    """

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="mig-")
        self.path = os.path.join(self.dir, "chat.db")
        self._saved = db_module.DATABASE_PATH
        db_module.DATABASE_PATH = self.path

    def tearDown(self):
        db_module.DATABASE_PATH = self._saved
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_foreign_table_with_the_same_name_is_moved_aside(self):
        """
        이름은 같은데 **설계가 다른** 표가 이미 있을 때.

        운영 서버에서 실제로 났다. 그 DB 에는 예전 설계의 relay_agents 가
        남아 있었다(token_hash / hosts_json / is_active). CREATE TABLE IF NOT
        EXISTS 는 그 표를 건너뛰므로, 뒤이어 도는 ALTER 와 UPDATE 가
        "no such column: revoked_at" 으로 깨지고 서버가 기동하지 못했다.

        고친 뒤 기대하는 것
          - 옛 표는 **지우지 않는다.** 이름만 비켜 둔다. 안에 무엇이 들어
            있는지 우리는 모른다.
          - 새 표는 제대로 만들어진다.
          - 옛 인덱스가 이름을 붙잡고 있어서 새 인덱스가 안 생기는 일이 없다.
        """
        c = db_module.connect(self.path)
        try:
            c.executescript(V1_SCHEMA)
            c.executescript(LEGACY_RELAY)
            c.execute("INSERT INTO relay_agents (name, token_hash, created_at)"
                      " VALUES ('VDI-OLD','deadbeef','2026-09-01 10:00:00')")
            c.execute("PRAGMA user_version = 1")
            c.commit()
        finally:
            c.close()

        db_module.migrate(verbose=False)

        c = db_module.connect(self.path)
        try:
            # 새 표가 제대로 섰다
            cols = db_module.column_names(c, "relay_agents")
            for want in ("key_hash", "owner_id", "revoked_at", "registered_at"):
                self.assertIn(want, cols, want)

            # 옛 표는 이름만 바뀐 채 **행까지 그대로** 있다
            moved = [r["name"] for r in c.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
                " AND name LIKE 'relay_agents_old_%'")]
            self.assertEqual(len(moved), 1, moved)
            self.assertEqual(
                c.execute('SELECT COUNT(*) FROM "%s"' % moved[0]).fetchone()[0], 1,
                "비켜 둔 표의 내용이 사라졌다")
            self.assertIn("token_hash", db_module.column_names(c, moved[0]))

            # 새 인덱스가 옛 이름에 막히지 않았다
            idx = {r["name"] for r in c.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
                " AND tbl_name = 'relay_agents'")}
            self.assertIn("idx_relay_agents_owner", idx, idx)

            self.assertEqual(c.execute("PRAGMA user_version").fetchone()[0], 9)
            self.assertEqual(c.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(c.execute("PRAGMA foreign_key_check").fetchall(), [])
        finally:
            c.close()

    def test_normal_v8_tables_are_not_mistaken_for_foreign(self):
        """
        **정상적인 v8 DB 를 남의 표로 몰면 안 된다.**

        v8 의 relay_agents 에는 owner_id 가 없다. v9 가 ALTER 로 더한다.
        없는 칸으로 판단하면 멀쩡한 운영 DB 의 중계 기록이 통째로 비켜나간다.
        """
        c = db_module.connect(self.path)
        try:
            c.executescript(V1_SCHEMA)
            # v9 이전의 relay_agents : owner_id 만 없다
            c.execute("CREATE TABLE relay_agents ("
                      " id INTEGER PRIMARY KEY AUTOINCREMENT,"
                      " key_hash TEXT NOT NULL UNIQUE,"
                      " name TEXT NOT NULL DEFAULT '',"
                      " version TEXT NOT NULL DEFAULT '',"
                      " os_info TEXT NOT NULL DEFAULT '',"
                      " ip TEXT NOT NULL DEFAULT '',"
                      " scheme TEXT NOT NULL DEFAULT '',"
                      " registered_at TEXT NOT NULL,"
                      " last_seen_at TEXT, revoked_at TEXT, created_by INTEGER)")
            c.execute("INSERT INTO relay_agents (key_hash, name, registered_at)"
                      " VALUES ('abc','VDI-01','2026-09-01 10:00:00')")
            c.execute("PRAGMA user_version = 8")
            c.commit()
        finally:
            c.close()

        db_module.migrate(verbose=False)

        c = db_module.connect(self.path)
        try:
            moved = [r["name"] for r in c.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
                " AND name LIKE 'relay_agents_old_%'")]
            self.assertEqual(moved, [], "멀쩡한 v8 표를 남의 표로 몰았다")
            self.assertIn("owner_id", db_module.column_names(c, "relay_agents"))
            row = c.execute("SELECT name, revoked_at FROM relay_agents").fetchone()
            self.assertEqual(row["name"], "VDI-01", "기록이 사라졌다")
            self.assertIsNotNone(row["revoked_at"],
                                 "주인 없는 중계는 올라올 때 끊어야 한다")
        finally:
            c.close()

    def test_repeated_failed_boots_do_not_push_out_old_backups(self):
        """
        기동이 반복해서 실패해도 **사고 전 백업이 남아 있어야 한다.**

        운영 서버에서 마이그레이션이 깨져 컨테이너가 재시작 루프에 빠졌고,
        1분에 한 번씩 백업이 생겼다. prune_backups(keep=10) 는 오래된 것부터
        지우므로, 그대로 두면 열 번 만에 사고 전 백업이 전부 밀려 나간다.
        되돌릴 것이 필요한 바로 그 순간에 되돌릴 것이 없어진다.

        실패한 마이그레이션은 롤백되므로 DB 는 그대로다. 그러니 두 번째부터의
        백업은 앞의 것과 내용이 같고, 같으면 새로 만들지 않아야 한다.
        """
        saved_dir = db_module.BACKUP_DIR
        db_module.BACKUP_DIR = os.path.join(self.dir, "backups")
        try:
            c = db_module.connect(self.path)
            try:
                c.executescript(V1_SCHEMA)
                c.execute("INSERT INTO projects (name,description,created_at,"
                          "updated_at) VALUES ('옛','',?,?)", ("t", "t"))
                c.commit()
            finally:
                c.close()

            # 사고 전에 손으로 받아 둔 백업 한 장
            keepsake = db_module.backup_database("manual")
            self.assertIsNotNone(keepsake)

            # 기동이 열두 번 실패한다 = DB 는 그대로인 채 백업만 열두 번 시도
            paths = [db_module.backup_database_once("migrate") for _ in range(12)]
            db_module.prune_backups()

            names = sorted(os.listdir(db_module.BACKUP_DIR))
            self.assertTrue(os.path.exists(keepsake),
                            "사고 전 백업이 밀려 나갔다: %s" % names)
            migrates = [n for n in names if "-migrate-" in n]
            self.assertEqual(len(migrates), 1,
                             "같은 내용인데 여러 장 쌓였다: %s" % migrates)
            self.assertEqual(len(set(paths)), 1,
                             "같은 내용이면 같은 경로를 돌려줘야 한다")

            # DB 가 실제로 바뀌면 그때는 새로 남긴다
            c = db_module.connect(self.path)
            try:
                c.execute("INSERT INTO projects (name,description,created_at,"
                          "updated_at) VALUES ('새','',?,?)", ("t", "t"))
                c.commit()
            finally:
                c.close()
            after = db_module.backup_database_once("migrate")
            self.assertNotEqual(after, paths[0], "바뀌었는데 새로 안 남겼다")
        finally:
            db_module.BACKUP_DIR = saved_dir

    def test_fresh(self):
        db_module.migrate(verbose=False)
        c = db_module.connect(self.path)
        try:
            self.assertEqual(c.execute("PRAGMA user_version").fetchone()[0], 9)
            for t in ("ssh_servers", "ssh_grants", "ssh_commands", "relay_agents",
                      "relay_enroll_codes", "relay_jobs", "term_sessions",
                      "term_inputs"):
                self.assertTrue(db_module.table_exists(c, t), t)
            self.assertIn("server_id", db_module.column_names(c, "sessions"))
            self.assertIn("ssh_level", db_module.column_names(c, "users"))
            self.assertEqual(c.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        finally:
            c.close()
        self.assertFalse(db_module.pending_migrations(db_module.connect(self.path)))

    def test_from_v1_keeps_data(self):
        raw = sqlite3.connect(self.path)
        raw.executescript(V1_SCHEMA)
        raw.execute("INSERT INTO projects (name,description,created_at,updated_at)"
                    " VALUES ('old','',datetime('now'),datetime('now'))")
        raw.execute("INSERT INTO sessions (project_id,name,created_at,updated_at)"
                    " VALUES (1,'옛 대화',datetime('now'),datetime('now'))")
        raw.execute("INSERT INTO messages (session_id,role,content,created_at)"
                    " VALUES (1,'user','옛 질문',datetime('now'))")
        raw.commit()
        raw.close()

        info = db_module.migrate(verbose=False)
        self.assertTrue(info["applied"])
        c = db_module.connect(self.path)
        try:
            # 기존 데이터가 그대로 있다
            self.assertEqual(c.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 1)
            row = c.execute("SELECT * FROM sessions WHERE id = 1").fetchone()
            self.assertEqual(row["name"], "옛 대화")
            # 로그인 없던 시절의 대화는 공개로 올라온다 (보던 사람이 계속 본다)
            self.assertEqual(row["visibility"], "public")
            self.assertIsNone(row["owner_id"])
            # 서버는 아직 아무것도 안 붙어 있다
            self.assertIsNone(row["server_id"])
            self.assertEqual(c.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(c.execute("PRAGMA foreign_key_check").fetchall(), [])
        finally:
            c.close()

    def test_from_v7_rebuilds_menu_check(self):
        # v8 까지 올린 뒤 user_menus 만 옛 제약으로 되돌린다
        db_module.migrate(verbose=False)
        c = db_module.connect(self.path)
        try:
            c.execute("INSERT INTO users (username,password_hash,display_name,role,"
                      "is_active,created_at,updated_at) VALUES"
                      " ('u','h','U','user',1,datetime('now'),datetime('now'))")
            c.execute("INSERT INTO user_menus (user_id,menu_key,granted_at)"
                      " VALUES (1,'chat',datetime('now'))")
            c.execute("INSERT INTO user_menus (user_id,menu_key,granted_at)"
                      " VALUES (1,'notes',datetime('now'))")
            c.commit()
            rows = c.execute("SELECT user_id,menu_key,granted_at,granted_by"
                             " FROM user_menus").fetchall()
            c.execute("PRAGMA foreign_keys = OFF")
            c.execute("DROP TABLE user_menus")
            c.execute("CREATE TABLE user_menus ("
                      " user_id INTEGER NOT NULL,"
                      " menu_key TEXT NOT NULL CHECK (menu_key IN"
                      "   ('chat','notes','patch')),"
                      " granted_at TEXT NOT NULL, granted_by INTEGER,"
                      " PRIMARY KEY (user_id, menu_key))")
            c.executemany("INSERT INTO user_menus VALUES (?,?,?,?)",
                          [tuple(r) for r in rows])
            c.execute("PRAGMA user_version = 7")
            c.commit()
        finally:
            c.close()

        self.assertIn("user_menus 재작성 (서버 메뉴 키 허용)",
                      db_module.pending_migrations(db_module.connect(self.path)))
        info = db_module.migrate(verbose=False)
        self.assertTrue(any("user_menus 재작성" in s for s in info["steps"]))

        c = db_module.connect(self.path)
        try:
            # 기존 권한이 그대로 남았다
            keys = {r["menu_key"] for r in c.execute("SELECT menu_key FROM user_menus")}
            self.assertEqual(keys, {"chat", "notes"})
            # 새 키가 들어간다
            c.execute("INSERT INTO user_menus (user_id,menu_key,granted_at)"
                      " VALUES (1,'servers',datetime('now'))")
            c.commit()
            self.assertFalse(db_module.table_exists(c, "user_menus_old"))
            self.assertEqual(c.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        finally:
            c.close()

    def test_twice_is_a_no_op(self):
        db_module.migrate(verbose=False)
        again = db_module.migrate(verbose=False)
        self.assertFalse(again["applied"])
        self.assertEqual(again["steps"], [])

    def test_server_delete_does_not_break_sessions(self):
        """
        대화가 붙어 있는 서버를 지워도 대화는 남는다.

        sessions.server_id 는 ALTER 로 더한 컬럼이라 ON DELETE 규칙이 없다.
        그래서 지우는 코드가 먼저 떼어 낸다. 그 약속이 깨지면 여기서 걸린다.
        """
        db_module.migrate(verbose=False)
        c = db_module.connect(self.path)
        try:
            c.execute("INSERT INTO projects (name,description,created_at,updated_at)"
                      " VALUES ('p','',datetime('now'),datetime('now'))")
            c.execute("INSERT INTO ssh_servers (name,host,port,username,auth_kind,"
                      "created_at,updated_at) VALUES"
                      " ('s','h',22,'u','key',datetime('now'),datetime('now'))")
            c.execute("INSERT INTO sessions (project_id,name,visibility,server_id,"
                      "created_at,updated_at) VALUES"
                      " (1,'n','private',1,datetime('now'),datetime('now'))")
            c.commit()
            with self.assertRaises(sqlite3.IntegrityError):
                c.execute("DELETE FROM ssh_servers WHERE id = 1")
                c.commit()
            c.rollback()
            # 떼어 낸 뒤에는 지워진다 (relay.delete_server 가 하는 순서)
            c.execute("UPDATE sessions SET server_id = NULL WHERE server_id = 1")
            c.execute("DELETE FROM ssh_servers WHERE id = 1")
            c.commit()
            self.assertEqual(c.execute("SELECT COUNT(*) FROM sessions").fetchone()[0], 1)
        finally:
            c.close()


# ---------------------------------------------------------------------------
# 3. 청소
# ---------------------------------------------------------------------------
class TestHousekeep(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        """이 반만 따로 돌려도 되게 사람을 하나 만들어 둔다. (user_id FK)"""
        c = conn()
        try:
            if not auth.user_by_name(c, "admin"):
                auth.create_user(c, "admin", "admin-pw-12345", "관리자", role="admin")
            c.commit()
        finally:
            c.close()

    def setUp(self):
        self.c = conn()
        self.c.execute("DELETE FROM relay_jobs")
        self.c.execute("DELETE FROM ssh_commands")
        self.c.execute("UPDATE sessions SET server_id = NULL")
        self.c.execute("DELETE FROM term_inputs")
        self.c.execute("DELETE FROM term_sessions")
        self.c.execute("DELETE FROM ssh_servers")
        self.c.execute("DELETE FROM relay_agents")
        self.c.commit()
        for t in store.HUB.all():
            store.HUB.drop(t.id)

    def tearDown(self):
        self.c.close()

    def _uid(self):
        c = conn()
        try:
            return auth.user_by_name(c, "admin")["id"]
        finally:
            c.close()

    def _server(self):
        cur = self.c.execute(
            "INSERT INTO ssh_servers (name,host,port,username,auth_kind,"
            "created_at,updated_at) VALUES ('hk','h',22,'u','key',?,?)",
            (db_module.ts(), db_module.ts()))
        self.c.commit()
        return cur.lastrowid

    def test_pending_card_expires(self):
        sid = self._server()
        cid = store.create_command(self.c, sid, None, None, self._uid(), "rm -f /x",
                                   "write", "pending")
        self.c.execute("UPDATE ssh_commands SET created_at ="
                       " datetime('now','localtime','-999 minutes') WHERE id = ?",
                       (cid,))
        self.c.commit()
        store.housekeep(self.c)
        row = store.get_command(self.c, cid)
        self.assertEqual(row["state"], "expired")
        self.assertIn("승인", row["reason"])

    def test_quiet_terminal_is_closed(self):
        sid = self._server()
        store.open_term_row(self.c, "quiet-1", sid, self._uid())
        self.c.commit()
        stream = store.HUB.create("quiet-1", sid, self._uid())
        stream.push("prompt$ ")
        # 브라우저가 오래 조용했다고 꾸민다
        stream.last_browser = time.time() - 9999
        store.housekeep(self.c)
        self.assertEqual(stream.state, "closed")
        row = store.term_row(self.c, "quiet-1")
        self.assertEqual(row["state"], "closed")
        self.assertTrue(row["close_reason"])

    def test_queue_is_canceled_without_a_relay(self):
        sid = self._server()
        job = store.enqueue(self.c, store.KIND_RUN, self._uid(), server_id=sid,
                            payload={"command": "df -h"})
        self.c.execute("UPDATE relay_jobs SET created_at ="
                       " datetime('now','localtime','-5 minutes') WHERE id = ?",
                       (job,))
        self.c.commit()
        store.housekeep(self.c)
        row = store.get_job(self.c, job)
        self.assertEqual(row["state"], "canceled")
        self.assertIn("중계", row["result"])

    def test_live_terminal_survives(self):
        """
        살아 있는 터미널을 청소가 닫아 버리면 쓰는 중에 끊긴다.

        "살아 있다" 는 **그 사람의** 중계가 붙어 있다는 뜻이다. 중계는 사람마다
        하나이므로 주인이 맞아야 한다.
        """
        sid = self._server()
        store.open_term_row(self.c, "live-1", sid, self._uid())
        self.c.execute("INSERT INTO relay_agents (key_hash,owner_id,name,"
                       "registered_at,last_seen_at) VALUES ('hk-hash',?,'a',?,?)",
                       (self._uid(), db_module.ts(), db_module.ts()))
        self.c.commit()
        stream = store.HUB.create("live-1", sid, self._uid())
        stream.push("prompt$ ")
        store.housekeep(self.c)
        self.assertNotEqual(stream.state, "closed")

    def test_orphan_terminals_are_closed_at_boot(self):
        """
        프로세스가 다시 뜨면 메모리의 화면 버퍼는 사라진다. DB 에 '열림' 으로
        남은 줄은 살아 있을 수 없고, 그대로 두면 "한 사람 2개" 자리를 유령이
        차지해서 다음에 터미널을 못 연다.
        """
        sid = self._server()
        store.open_term_row(self.c, "orphan-1", sid, self._uid())
        store.set_term_state(self.c, "orphan-1", "open")
        self.c.commit()
        self.assertEqual(store.open_term_count(self.c, self._uid()), 1)

        n = store.close_orphan_terms(self.c)      # 기동할 때 하는 일
        self.assertEqual(n, 1)
        self.assertEqual(store.open_term_count(self.c, self._uid()), 0)
        row = store.term_row(self.c, "orphan-1")
        self.assertEqual(row["state"], "closed")
        self.assertIn("다시 시작", row["close_reason"])

    def test_owner_less_relay_takes_nothing(self):
        """
        주인 없는 중계(v9 이전에 등록된 것)는 아무 일도 가져가지 못한다.

        일마다 주인이 붙기 때문이다. 마이그레이션이 그런 중계를 끊지만, 혹시
        남아 있더라도 남의 일을 가져가서는 안 된다.
        """
        sid = self._server()
        cur = self.c.execute(
            "INSERT INTO relay_agents (key_hash,owner_id,name,registered_at,"
            "last_seen_at) VALUES ('no-owner-hash',NULL,'ghost',?,?)",
            (db_module.ts(), db_module.ts()))
        ghost = cur.lastrowid
        store.enqueue(self.c, store.KIND_RUN, self._uid(), server_id=sid,
                      payload={"command": "df -h"})
        self.c.commit()

        took = store.take_jobs(self.c, ghost, None)
        self.assertEqual(took, [], "주인 없는 중계가 남의 일을 가져갔다")

    def test_side_channel_is_swept(self):
        """아무도 받아 가지 않은 출력은 메모리에 남지 않는다."""
        store.put_side("output", 999999, "left-behind")
        store._SIDE["output"][999999] = ("left-behind", time.time() - 99999)
        store.housekeep(self.c)
        self.assertIsNone(store.take_side("output", 999999))


if __name__ == "__main__":
    unittest.main(verbosity=2)
