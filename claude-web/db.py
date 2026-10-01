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

v3 -> v4 (메모 기능 추가)
-------------------------
notes / note_attachments 테이블을 **추가만** 한다. 기존 테이블의 컬럼이나 데이터는
전혀 건드리지 않으므로 채팅 기능에 영향이 없고, 실패해도 롤백되어 원래대로 남는다.
"""

import os
import shutil
import sqlite3
import stat
import time

from flask import g

from config import BACKUP_DIR, DATABASE_PATH, UPLOAD_DIR

SCHEMA_VERSION = 5

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

-- 메모 (v4)
-- 채팅과 완전히 독립적인 기능이다. sessions 와 같은 private/public 개념을 쓴다.
-- owner_id 를 ON DELETE SET NULL 로 둔 것은 sessions 와 같은 이유다. 관리자가
-- 사용자를 지울 때 그 사람이 쓴 글이 조용히 사라지지 않게 한다. 소유자가 없는
-- private 메모는 아무에게도 보이지 않지만 데이터는 남는다. (복구 가능)
CREATE TABLE IF NOT EXISTS notes (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id   INTEGER,
    title      TEXT NOT NULL DEFAULT '',
    content    TEXT NOT NULL DEFAULT '',
    visibility TEXT NOT NULL DEFAULT 'private'
               CHECK (visibility IN ('private', 'public')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (owner_id) REFERENCES users(id) ON DELETE SET NULL
);

-- 기존 attachments 를 범용으로 확장(session_id 를 nullable 로 바꾸고 종류 컬럼을
-- 추가)하는 방법도 검토했지만 택하지 않았다.
--   - attachments.session_id 는 NOT NULL + FK 다. sqlite 는 ALTER 로 NOT NULL 을
--     풀 수 없어 **운영 데이터가 들어 있는 테이블을 재생성**해야 한다.
--     (임시 테이블 생성 -> 복사 -> 삭제 -> 이름 변경)
--   - 재생성 중 실패하면 채팅 첨부파일 전체가 위험하다. 얻는 것보다 잃을 게 크다.
-- 그래서 구조만 같게 맞춘 별도 테이블을 만든다. 채팅 쪽은 한 줄도 바뀌지 않는다.
-- file_path 는 attachments 와 같은 규칙으로 **NOTES_DIR 기준 상대경로**를 넣는다.
-- (절대경로를 넣으면 DB 를 다른 서버로 옮길 때 전부 열리지 않는다. v2->v3 참고)
CREATE TABLE IF NOT EXISTS note_attachments (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    note_id       INTEGER NOT NULL,
    original_name TEXT NOT NULL,
    stored_name   TEXT NOT NULL,
    file_path     TEXT NOT NULL,
    mime_type     TEXT NOT NULL,
    file_size     INTEGER NOT NULL,
    created_at    TEXT NOT NULL,
    FOREIGN KEY (note_id) REFERENCES notes(id) ON DELETE CASCADE
);

-- 메모 댓글 (v5)
-- 대댓글은 parent_id 로 표현한다. 깊이는 한 단계로 제한한다. 즉 댓글에는
-- 답글을 달 수 있지만 답글에는 달 수 없다. (서버에서 검사한다)
-- 끝없이 들여쓰기가 깊어지면 좁은 화면에서 글을 읽을 수 없고, 권한과 삭제
-- 규칙도 따라서 복잡해진다. 사내 메모에 그만한 깊이가 필요하지 않다.
--
-- user_id 를 ON DELETE SET NULL 로 둔 이유는 notes/sessions 와 같다.
-- 관리자가 사용자를 지울 때 그 사람이 쓴 글이 조용히 사라지지 않게 한다.
-- parent_id 는 CASCADE 다. 부모 댓글을 지우면 그 아래 답글도 함께 사라지는
-- 것이 자연스럽다. (답글만 남아 맥락 없이 떠 있으면 읽을 수 없다)
CREATE TABLE IF NOT EXISTS note_comments (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    note_id    INTEGER NOT NULL,
    parent_id  INTEGER,
    user_id    INTEGER,
    content    TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (note_id) REFERENCES notes(id) ON DELETE CASCADE,
    FOREIGN KEY (parent_id) REFERENCES note_comments(id) ON DELETE CASCADE,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE SET NULL
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
CREATE INDEX IF NOT EXISTS idx_notes_owner        ON notes(owner_id);
CREATE INDEX IF NOT EXISTS idx_notes_updated      ON notes(updated_at);
CREATE INDEX IF NOT EXISTS idx_note_att_note      ON note_attachments(note_id);
CREATE INDEX IF NOT EXISTS idx_note_cmt_note      ON note_comments(note_id, id);
CREATE INDEX IF NOT EXISTS idx_note_cmt_parent    ON note_comments(parent_id);
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


# ---------------------------------------------------------------------------
# v2 -> v3 : attachments.file_path 를 UPLOAD_DIR 기준 상대경로로 바꾼다
#
# v2 까지는 절대경로를 넣었다. 그 DB 를 다른 서버로 옮기면 업로드 경로가 달라져
# (개발 PC 의 data/uploads -> /var/lib/claude-web/uploads) 첨부 API 의 경로 검사에
# 전부 걸려 이미지가 하나도 열리지 않는다.
# 저장 규칙이 project_<id>/session_<id>/<uuid>.<ext> 로 고정이므로 그 꼬리만 남긴다.
# 파일 자체는 건드리지 않는다. DB 에 적힌 표기만 옮긴다.
# ---------------------------------------------------------------------------
def is_absolute_path(value):
    """POSIX 절대경로와 Windows 경로를 실행 중인 OS 와 무관하게 판별한다."""
    v = str(value or "")
    if v[:1] in ("/", "\\"):
        return True
    return len(v) > 2 and v[1] == ":" and v[2] in ("/", "\\")


def to_relative_upload_path(value):
    """상대경로로 바꾼 값. 저장 규칙에 맞는 꼬리를 못 찾으면 None."""
    parts = [p for p in str(value or "").replace("\\", "/").split("/") if p]
    for i, name in enumerate(parts):
        if name.startswith("project_") and name[len("project_"):].isdigit():
            tail = parts[i:]
            if len(tail) >= 2:
                return "/".join(tail)
    return None


def legacy_attachment_paths(conn):
    """고쳐야 할 행을 [(id, 상대경로), ...] 로 돌려준다."""
    if not table_exists(conn, "attachments"):
        return []
    out = []
    for r in conn.execute("SELECT id, file_path FROM attachments"):
        if not is_absolute_path(r["file_path"]):
            continue
        rel = to_relative_upload_path(r["file_path"])
        if rel:
            out.append((r["id"], rel))
    return out


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
    for t in ("users", "settings", "session_members", "audit_logs", "login_attempts",
              "notes", "note_attachments", "note_comments"):
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
    n = len(legacy_attachment_paths(conn))
    if n:
        todo.append("attachments.file_path %d건을 상대경로로 변환" % n)
    return todo


def has_data(conn):
    for t in ("projects", "sessions", "messages", "notes"):
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

            legacy = legacy_attachment_paths(conn)
            if legacy:
                conn.executemany(
                    "UPDATE attachments SET file_path = ? WHERE id = ?",
                    [(rel, aid) for aid, rel in legacy])
                steps.append(
                    "attachments.file_path %d건을 상대경로로 변환 (기준: %s)"
                    % (len(legacy), UPLOAD_DIR))

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
