#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
db
==

SQLite 연결 / 스키마 / 마이그레이션.

마이그레이션 원칙
-----------------
- 기존 DB 를 절대 지우지 않는다. 필요한 테이블/컬럼만 추가한다.
- 변경이 필요할 때만 자동으로 백업을 먼저 남긴다. (data/backups/)
- 모든 DDL/DML 을 하나의 트랜잭션에서 실행한다. 중간에 실패하면 전부 롤백되어
  기존 DB 가 그대로 남는다.
- PRAGMA user_version 으로 적용 여부를 기록한다.

v1 -> v2 (로그인/권한 도입) 에서 기존 데이터 처리
-------------------------------------------------
v1 에는 로그인이 없었다. 즉 기존 프로젝트/세션은 "그 서버에 접근할 수 있는
모든 사람이 보던 것"이다. 그래서 기존 세션은

    owner_id   = NULL   ("레거시", 작성자를 알 수 없음)
    visibility = public (기존과 동일하게 모두가 볼 수 있음)

으로 옮긴다. 임의의 admin 을 소유자로 지정하면 (1) 그 사람이 쓰지 않은 대화의
소유자가 되고 (2) private 로 바뀌면 이전에 보던 사람들이 못 보게 되므로
기존 동작을 그대로 보존하는 이 방식을 택했다.

owner 가 NULL 인 세션은 이름 변경/삭제를 일반 사용자가 할 수 없고, 관리자가
관리자 페이지에서 소유자를 지정하거나 삭제할 수 있다.
"""

import os
import shutil
import sqlite3
import stat
import time

from flask import g

from config import BACKUP_DIR, DATABASE_PATH

SCHEMA_VERSION = 2

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    display_name  TEXT NOT NULL DEFAULT '',
    role          TEXT NOT NULL DEFAULT 'user' CHECK (role IN ('admin', 'user')),
    is_active     INTEGER NOT NULL DEFAULT 1,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    last_login_at TEXT
);

CREATE TABLE IF NOT EXISTS settings (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL,
    updated_by INTEGER,
    FOREIGN KEY (updated_by) REFERENCES users(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS projects (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id        INTEGER NOT NULL,
    owner_id          INTEGER,
    name              TEXT NOT NULL,
    visibility        TEXT NOT NULL DEFAULT 'private'
                      CHECK (visibility IN ('private', 'public')),
    claude_session_id TEXT,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL,
    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE,
    FOREIGN KEY (owner_id) REFERENCES users(id) ON DELETE SET NULL
);

-- 지금은 private/public 만 쓰지만, 나중에 "특정 사용자에게만 공유(shared)" 를
-- 붙일 때 스키마 변경 없이 쓰도록 미리 만들어 둔다.
CREATE TABLE IF NOT EXISTS session_members (
    session_id INTEGER NOT NULL,
    user_id    INTEGER NOT NULL,
    permission TEXT NOT NULL DEFAULT 'read' CHECK (permission IN ('read', 'write')),
    created_at TEXT NOT NULL,
    PRIMARY KEY (session_id, user_id),
    FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS messages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL,
    user_id    INTEGER,
    role       TEXT NOT NULL CHECK (role IN ('user', 'assistant', 'error')),
    content    TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS attachments (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id    INTEGER NOT NULL,
    message_id    INTEGER,
    original_name TEXT NOT NULL,
    stored_name   TEXT NOT NULL,
    file_path     TEXT NOT NULL,
    mime_type     TEXT NOT NULL,
    file_size     INTEGER NOT NULL,
    created_at    TEXT NOT NULL,
    FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE,
    FOREIGN KEY (message_id) REFERENCES messages(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS audit_logs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER,
    action      TEXT NOT NULL,
    target_type TEXT NOT NULL DEFAULT '',
    target_id   TEXT NOT NULL DEFAULT '',
    details     TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS login_attempts (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    username   TEXT NOT NULL DEFAULT '',
    ip         TEXT NOT NULL DEFAULT '',
    success    INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_sessions_project   ON sessions(project_id);
CREATE INDEX IF NOT EXISTS idx_sessions_owner     ON sessions(owner_id);
CREATE INDEX IF NOT EXISTS idx_messages_session   ON messages(session_id, id);
CREATE INDEX IF NOT EXISTS idx_attachments_message ON attachments(message_id);
CREATE INDEX IF NOT EXISTS idx_attachments_session ON attachments(session_id);
CREATE INDEX IF NOT EXISTS idx_audit_created      ON audit_logs(created_at);
CREATE INDEX IF NOT EXISTS idx_login_attempts     ON login_attempts(created_at);
"""


def connect(path=None):
    conn = sqlite3.connect(path or DATABASE_PATH, timeout=15.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 15000")
    return conn


def get_db():
    if "db" not in g:
        g.db = connect()
    return g.db


def close_db(_exc=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def ts():
    return time.strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------------------
# 스키마 점검 helper
# ---------------------------------------------------------------------------
def table_exists(conn, name):
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def column_names(conn, table):
    if not table_exists(conn, table):
        return set()
    return {r["name"] for r in conn.execute("PRAGMA table_info(%s)" % table)}


def harden_permissions():
    """DB 파일에 자격증명/설정이 들어가므로 소유자만 읽도록 제한한다. (POSIX)"""
    if os.name != "posix":
        return
    for path in (DATABASE_PATH, DATABASE_PATH + "-wal", DATABASE_PATH + "-shm"):
        try:
            if os.path.exists(path):
                os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# 백업
# ---------------------------------------------------------------------------
def backup_database(label="manual"):
    """
    WAL 을 쓰므로 단순 파일 복사 대신 sqlite 의 온라인 백업 API 를 쓴다.
    실행 중에도 일관된 스냅샷을 만든다. 반환: 백업 파일 경로 또는 None
    """
    if not os.path.exists(DATABASE_PATH):
        return None
    os.makedirs(BACKUP_DIR, exist_ok=True)
    dest = os.path.join(
        BACKUP_DIR,
        "chat.db.backup-%s-%s" % (label, time.strftime("%Y%m%d-%H%M%S")),
    )
    src = sqlite3.connect(DATABASE_PATH, timeout=30.0)
    try:
        dst = sqlite3.connect(dest)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    if os.name == "posix":
        try:
            os.chmod(dest, stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass
    return dest


def prune_backups(keep=10):
    try:
        names = sorted(
            n for n in os.listdir(BACKUP_DIR) if n.startswith("chat.db.backup-"))
    except OSError:
        return
    for name in names[:-keep] if len(names) > keep else []:
        try:
            os.remove(os.path.join(BACKUP_DIR, name))
        except OSError:
            pass


# ---------------------------------------------------------------------------
# 마이그레이션
# ---------------------------------------------------------------------------
def pending_migrations(conn):
    """적용해야 할 변경 목록을 사람이 읽을 수 있는 문자열로 돌려준다."""
    todo = []
    for t in ("users", "settings", "session_members", "audit_logs", "login_attempts"):
        if not table_exists(conn, t):
            todo.append("CREATE TABLE %s" % t)
    if table_exists(conn, "sessions"):
        cols = column_names(conn, "sessions")
        if "owner_id" not in cols:
            todo.append("sessions.owner_id 추가")
        if "visibility" not in cols:
            todo.append("sessions.visibility 추가")
    if table_exists(conn, "messages") and "user_id" not in column_names(conn, "messages"):
        todo.append("messages.user_id 추가")
    return todo


def has_data(conn):
    for t in ("projects", "sessions", "messages"):
        if table_exists(conn, t):
            if conn.execute("SELECT 1 FROM %s LIMIT 1" % t).fetchone():
                return True
    return False


def migrate(verbose=True):
    """
    스키마를 최신 상태로 맞춘다. 반환: dict(applied, backup, steps)
    이미 최신이면 아무것도 하지 않는다. (매 기동 시 호출해도 안전)
    """
    os.makedirs(os.path.dirname(DATABASE_PATH), exist_ok=True)
    conn = connect()
    steps, backup_path = [], None
    try:
        fresh = not table_exists(conn, "sessions")
        todo = pending_migrations(conn)
        needs_work = bool(todo)

        # 기존 데이터가 있는 DB 를 고치는 경우에만 백업한다.
        if needs_work and not fresh and has_data(conn):
            conn.close()
            backup_path = backup_database("migrate")
            prune_backups()
            conn = connect()
            steps.append("백업: %s" % backup_path)

        # WAL 설정은 트랜잭션 밖에서
        conn.execute("PRAGMA journal_mode = WAL")

        # sqlite 의 DDL 은 트랜잭션에 포함된다. 중간 실패 시 통째로 롤백된다.
        conn.execute("BEGIN")
        try:
            # executescript 는 암묵적 커밋을 하므로 트랜잭션을 유지하려면 쓰면 안 된다.
            # 인덱스는 반드시 ALTER TABLE 로 컬럼을 추가한 "뒤에" 만들어야 한다.
            # (기존 DB 의 sessions 에는 아직 owner_id 가 없다)
            stmts = [x.strip() for x in SCHEMA.split(";") if x.strip()]
            creates = [x for x in stmts if not x.upper().lstrip().startswith("CREATE INDEX")]
            indexes = [x for x in stmts if x.upper().lstrip().startswith("CREATE INDEX")]

            for stmt in creates:
                conn.execute(stmt)

            if not fresh:
                cols = column_names(conn, "sessions")
                legacy_upgrade = "visibility" not in cols
                if "owner_id" not in cols:
                    conn.execute(
                        "ALTER TABLE sessions ADD COLUMN owner_id INTEGER "
                        "REFERENCES users(id)")
                    steps.append("sessions.owner_id 추가 (기존 행은 NULL = 레거시)")
                if legacy_upgrade:
                    conn.execute(
                        "ALTER TABLE sessions ADD COLUMN visibility TEXT NOT NULL "
                        "DEFAULT 'private' CHECK (visibility IN ('private', 'public'))")
                    n = conn.execute(
                        "UPDATE sessions SET visibility = 'public' "
                        "WHERE owner_id IS NULL").rowcount
                    steps.append(
                        "sessions.visibility 추가, 기존 세션 %d개를 public 으로 이관" % n)

                if "user_id" not in column_names(conn, "messages"):
                    conn.execute(
                        "ALTER TABLE messages ADD COLUMN user_id INTEGER "
                        "REFERENCES users(id)")
                    steps.append("messages.user_id 추가 (기존 행은 NULL)")

            for stmt in indexes:
                conn.execute(stmt)

            conn.execute("PRAGMA user_version = %d" % SCHEMA_VERSION)
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()

    harden_permissions()
    if verbose and steps:
        for s in steps:
            print("[migrate] %s" % s)
    return {"applied": bool(steps), "backup": backup_path, "steps": steps}


def row_to_dict(row):
    return {k: row[k] for k in row.keys()}


def audit(db, user_id, action, target_type="", target_id="", details=""):
    """
    운영상 중요한 변경만 기록한다.
    메시지 본문, 비밀번호, API key 같은 민감정보는 절대 넣지 않는다.
    """
    db.execute(
        "INSERT INTO audit_logs (user_id, action, target_type, target_id, details, created_at)"
        " VALUES (?,?,?,?,?,?)",
        (user_id, action, str(target_type), str(target_id), str(details)[:500], ts()),
    )


def copy_file_backup():
    """`cp chat.db chat.db.backup-...` 에 해당하는 단순 복사본. (수동 명령용)"""
    dest = DATABASE_PATH + ".backup-" + time.strftime("%Y%m%d-%H%M")
    shutil.copy2(DATABASE_PATH, dest)
    return dest
