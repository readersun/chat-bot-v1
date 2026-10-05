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

v8 -> v9 (중계를 사람마다)
-------------------------
relay_agents.owner_id 와 relay_jobs.owner_id 를 더한다. 사람마다 자기 VDI 에
중계를 깔기 때문에, 일이 **그 사람의 중계로만** 나가야 한다. 컬럼 두 개를
더하는 것뿐이고 기존 데이터는 건드리지 않는다.

v7 -> v8 (SSH 중계 추가)
------------------------
ssh_* / relay_* / term_* 표를 **추가만** 하고, 기존 표에는 컬럼 세 개를 더한다.
(sessions.server_id, users.ssh_all_servers, users.ssh_level)

하나 예외가 있다. user_menus.menu_key 의 CHECK 제약에 'servers' 를 넣어야 하는데
sqlite 는 제약만 바꾸는 ALTER 가 없다. 그래서 그 표만 "이름 바꾸기 → 새로 만들기
→ 옮겨 담기 → 버리기" 로 다시 만든다. 전부 같은 트랜잭션 안이라 중간에 실패하면
원래 표가 그대로 남는다. 옮길 때 지금 쓰는 메뉴 키만 남기므로 권한이 늘지 않는다.
"""

import os
import shutil
import sqlite3
import stat
import time

from flask import g

import patch_rules
from config import BACKUP_DIR, DATABASE_PATH, UPLOAD_DIR

SCHEMA_VERSION = 9

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

-- ---------------------------------------------------------------------------
-- 메뉴 권한 (v6)
--
-- 행이 있으면 그 메뉴가 보인다. 관리자는 이 표와 무관하게 전부 가진다.
-- 레일에서 안 그리는 것만으로는 권한이 아니다. 라우트마다 서버에서 막는다.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS user_menus (
    user_id    INTEGER NOT NULL,
    menu_key   TEXT NOT NULL CHECK (menu_key IN ('chat', 'notes', 'patch', 'servers')),
    granted_at TEXT NOT NULL,
    granted_by INTEGER,
    PRIMARY KEY (user_id, menu_key),
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
    FOREIGN KEY (granted_by) REFERENCES users(id) ON DELETE SET NULL
);

-- ---------------------------------------------------------------------------
-- 패치 저장소 (v6)
--
-- 파일시스템이 진짜고 이 표들은 색인이다. 통째로 지워도 스캔 한 번으로 전부
-- 돌아와야 한다. 그래서 어느 표에도 "파일에 없는 정보"를 넣지 않는다.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS patch_version_rules (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL UNIQUE,
    pattern    TEXT NOT NULL,
    sort_kind  TEXT NOT NULL DEFAULT 'numeric'
               CHECK (sort_kind IN ('numeric', 'lexical')),
    sample     TEXT NOT NULL DEFAULT '',
    is_builtin INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- 와칭 루트. 관리자가 손으로 등록하는 유일한 경로다. 여러 개 둘 수 있다.
CREATE TABLE IF NOT EXISTS patch_roots (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    label           TEXT NOT NULL DEFAULT '',
    path            TEXT NOT NULL UNIQUE,
    scan_enabled    INTEGER NOT NULL DEFAULT 1,
    scan_interval_s INTEGER NOT NULL DEFAULT 600,
    date_format     TEXT NOT NULL DEFAULT 'YYMMDD',
    hash_enabled    INTEGER NOT NULL DEFAULT 0,
    last_scanned_at TEXT,
    last_error      TEXT NOT NULL DEFAULT '',
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

-- 사이트 = 루트 바로 아래 폴더. 스캔이 찾아 넣는다. 사람이 만들지 않는다.
-- 루트가 다르면 같은 이름의 사이트가 있어도 된다. 그래서 UNIQUE 가 둘이다.
CREATE TABLE IF NOT EXISTS patch_sites (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    root_id       INTEGER NOT NULL,
    name          TEXT NOT NULL,
    label         TEXT NOT NULL DEFAULT '',
    date_format   TEXT NOT NULL DEFAULT 'YYMMDD',
    is_visible    INTEGER NOT NULL DEFAULT 0,
    discovered_at TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    UNIQUE (root_id, name),
    FOREIGN KEY (root_id) REFERENCES patch_roots(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS patch_products (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    site_id         INTEGER NOT NULL,
    name            TEXT NOT NULL,
    label           TEXT NOT NULL DEFAULT '',
    version_rule_id INTEGER,
    is_visible      INTEGER NOT NULL DEFAULT 0,
    sort_order      INTEGER NOT NULL DEFAULT 0,
    discovered_at   TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    UNIQUE (site_id, name),
    FOREIGN KEY (site_id) REFERENCES patch_sites(id) ON DELETE CASCADE,
    FOREIGN KEY (version_rule_id) REFERENCES patch_version_rules(id) ON DELETE SET NULL
);

-- 모듈만은 손으로 등록한다. "매칭된 파일만 버전 관리" 라는 규칙이 여기 걸려 있다.
-- name 은 파일 이름 앞부분과 글자 그대로 같아야 한다.
CREATE TABLE IF NOT EXISTS patch_modules (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id      INTEGER NOT NULL,
    name            TEXT NOT NULL,
    label           TEXT NOT NULL DEFAULT '',
    version_rule_id INTEGER,
    sort_order      INTEGER NOT NULL DEFAULT 0,
    -- is_active  = 스캐너가 이 모듈로 파일을 매칭할지. (API 전용, 화면 없음)
    -- is_visible = 사용자 패치 화면에 보일지. 숨겨도 매칭은 계속한다.
    --   둘을 나눈 이유: 숨기려고 매칭을 끄면 새로 들어온 파일이 "매칭 안 된
    --   파일" 로 쌓여서, 등록해 둔 모듈인데 등록하라는 목록에 뜬다.
    is_active       INTEGER NOT NULL DEFAULT 1,
    is_visible      INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    UNIQUE (product_id, name),
    FOREIGN KEY (product_id) REFERENCES patch_products(id) ON DELETE CASCADE,
    FOREIGN KEY (version_rule_id) REFERENCES patch_version_rules(id) ON DELETE SET NULL
);

-- 스캔이 찾은 tar 한 건. module_id 가 NULL 이면 "매칭 안 된 파일" 이다.
-- 지우지 않고 남겨 둬야 관리자가 새 모듈이 들어온 것을 알 수 있다.
CREATE TABLE IF NOT EXISTS patch_files (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id      INTEGER NOT NULL,
    module_id       INTEGER,
    date_dir        TEXT NOT NULL,
    date_at         TEXT,
    filename        TEXT NOT NULL,
    rel_path        TEXT NOT NULL,
    version         TEXT NOT NULL DEFAULT '',
    version_sort    TEXT NOT NULL DEFAULT '',
    suffix          TEXT NOT NULL DEFAULT '',
    size            INTEGER NOT NULL DEFAULT 0,
    mtime_ns        INTEGER NOT NULL DEFAULT 0,
    sha256          TEXT NOT NULL DEFAULT '',
    content_changed INTEGER NOT NULL DEFAULT 0,
    -- 관리자가 이 파일 한 건만 감춘 상태. 스캔은 이 칸을 건드리지 않는다.
    -- (다시 스캔해도 숨김이 풀리면 안 된다)
    is_visible      INTEGER NOT NULL DEFAULT 1,
    is_missing      INTEGER NOT NULL DEFAULT 0,
    first_seen_at   TEXT NOT NULL,
    last_seen_at    TEXT NOT NULL,
    UNIQUE (product_id, date_dir, filename),
    FOREIGN KEY (product_id) REFERENCES patch_products(id) ON DELETE CASCADE,
    FOREIGN KEY (module_id) REFERENCES patch_modules(id) ON DELETE SET NULL
);

-- 스캔 이력. 루트 하나가 한 번 도는 것이 한 줄이다.
-- trigger 는 SQLite 예약어라 trigger_kind 로 둔다.
CREATE TABLE IF NOT EXISTS patch_scans (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    root_id      INTEGER,
    trigger_kind TEXT NOT NULL DEFAULT 'manual'
                 CHECK (trigger_kind IN ('manual', 'periodic')),
    started_at   TEXT NOT NULL,
    finished_at  TEXT,
    added        INTEGER NOT NULL DEFAULT 0,
    updated      INTEGER NOT NULL DEFAULT 0,
    missing      INTEGER NOT NULL DEFAULT 0,
    held         INTEGER NOT NULL DEFAULT 0,
    unmatched    INTEGER NOT NULL DEFAULT 0,
    off_rule     INTEGER NOT NULL DEFAULT 0,
    new_sites    INTEGER NOT NULL DEFAULT 0,
    new_products INTEGER NOT NULL DEFAULT 0,
    error        TEXT NOT NULL DEFAULT '',
    FOREIGN KEY (root_id) REFERENCES patch_roots(id) ON DELETE SET NULL
);

-- 사이트 범위. users.patch_all_sites = 0 일 때만 읽는다.
CREATE TABLE IF NOT EXISTS user_patch_sites (
    user_id    INTEGER NOT NULL,
    site_id    INTEGER NOT NULL,
    granted_at TEXT NOT NULL,
    granted_by INTEGER,
    PRIMARY KEY (user_id, site_id),
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
    FOREIGN KEY (site_id) REFERENCES patch_sites(id) ON DELETE CASCADE,
    FOREIGN KEY (granted_by) REFERENCES users(id) ON DELETE SET NULL
);

-- ---------------------------------------------------------------------------
-- SSH 중계 (v8)
--
-- 챗봇 서버는 사내망 리눅스 서버에 직접 붙지 못한다. VDI 에 올려 둔 중계
-- 프로그램이 이쪽으로 들어와서(한 방향) 할 일을 받아 가고 결과를 올린다.
-- 그래서 이 표들은 "중계가 받아 갈 일"과 "그 결과"를 담는다.
--
-- 비밀은 두 군데에만 있다.
--   ssh_servers.secret_enc  : 비밀번호. settings_store 와 같은 Fernet 파생키.
--   relay_agents.key_hash   : 중계 토큰의 sha256. 원문은 등록 때 한 번만 준다.
-- 기록용 표(ssh_commands, term_inputs)에는 어떤 비밀도 넣지 않는다.
--
-- 지우는 연산을 두지 않는다. 기록을 손댈 수 있으면 기록이 아니다.
-- (relay_jobs 는 전송 큐라서 끝난 행만 보관 기간 뒤에 정리한다)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ssh_servers (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    name           TEXT NOT NULL UNIQUE,
    host           TEXT NOT NULL,
    port           INTEGER NOT NULL DEFAULT 22,
    username       TEXT NOT NULL,
    auth_kind      TEXT NOT NULL DEFAULT 'key' CHECK (auth_kind IN ('key', 'password')),
    key_name       TEXT NOT NULL DEFAULT '',
    secret_enc     TEXT NOT NULL DEFAULT '',
    description    TEXT NOT NULL DEFAULT '',
    is_enabled     INTEGER NOT NULL DEFAULT 1,
    last_check_at  TEXT,
    last_check_ok  INTEGER,
    last_check_msg TEXT NOT NULL DEFAULT '',
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL,
    created_by     INTEGER,
    FOREIGN KEY (created_by) REFERENCES users(id) ON DELETE SET NULL
);

-- 사용 허용 범위. users.ssh_all_servers = 0 일 때만 본다.
-- (패치의 user_patch_sites 와 같은 모양이다)
CREATE TABLE IF NOT EXISTS ssh_grants (
    user_id    INTEGER NOT NULL,
    server_id  INTEGER NOT NULL,
    granted_at TEXT NOT NULL,
    granted_by INTEGER,
    PRIMARY KEY (user_id, server_id),
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
    FOREIGN KEY (server_id) REFERENCES ssh_servers(id) ON DELETE CASCADE,
    FOREIGN KEY (granted_by) REFERENCES users(id) ON DELETE SET NULL
);

-- 중계 한 대 = 한 행. 토큰 원문은 저장하지 않는다.
--
-- owner_id 가 핵심이다. 사람마다 자기 VDI 에 중계를 깔고 **자기 키로** 서버에
-- 붙는다. 그래서 일은 그 사람의 중계로만 나가야 한다. 아무 중계나 남의 일을
-- 가져가면 "누구 권한으로 돈 명령인지" 가 흐려진다.
--
-- 한 사람에 한 대다. 다시 등록하면 그 사람의 예전 중계만 끊는다.
-- (남의 중계는 건드리지 않는다)
CREATE TABLE IF NOT EXISTS relay_agents (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    key_hash      TEXT NOT NULL UNIQUE,
    owner_id      INTEGER,
    name          TEXT NOT NULL DEFAULT '',
    version       TEXT NOT NULL DEFAULT '',
    os_info       TEXT NOT NULL DEFAULT '',
    ip            TEXT NOT NULL DEFAULT '',
    scheme        TEXT NOT NULL DEFAULT '',
    registered_at TEXT NOT NULL,
    last_seen_at  TEXT,
    revoked_at    TEXT,
    created_by    INTEGER,
    FOREIGN KEY (owner_id) REFERENCES users(id) ON DELETE CASCADE,
    FOREIGN KEY (created_by) REFERENCES users(id) ON DELETE SET NULL
);

-- 등록 코드. 한 번 쓰면 죽는다(used_at). 원문 대신 해시만 둔다.
CREATE TABLE IF NOT EXISTS relay_enroll_codes (
    code_hash  TEXT PRIMARY KEY,
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    created_by INTEGER,
    used_at    TEXT,
    agent_id   INTEGER,
    FOREIGN KEY (created_by) REFERENCES users(id) ON DELETE SET NULL,
    FOREIGN KEY (agent_id) REFERENCES relay_agents(id) ON DELETE SET NULL
);

-- 중계가 받아 갈 일. payload 에 비밀을 넣지 않는다. 접속 정보는 중계가
-- 일을 받아 가는 그 응답에만 실려 나가고 어디에도 적히지 않는다.
CREATE TABLE IF NOT EXISTS relay_jobs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    kind         TEXT NOT NULL
                 CHECK (kind IN ('test', 'run', 'term_open', 'term_input', 'term_close')),
    -- 이 일을 가져갈 수 있는 사람. 그 사람의 중계만 가져간다.
    -- (relay_store.take_jobs 가 agent 의 owner_id 와 맞춰 본다)
    owner_id     INTEGER,
    server_id    INTEGER,
    term_id      TEXT NOT NULL DEFAULT '',
    payload      TEXT NOT NULL DEFAULT '',
    state        TEXT NOT NULL DEFAULT 'queued'
                 CHECK (state IN ('queued', 'taken', 'done', 'failed', 'canceled')),
    agent_id     INTEGER,
    requested_by INTEGER,
    created_at   TEXT NOT NULL,
    taken_at     TEXT,
    done_at      TEXT,
    ok           INTEGER,
    result       TEXT NOT NULL DEFAULT '',
    FOREIGN KEY (owner_id) REFERENCES users(id) ON DELETE CASCADE,
    FOREIGN KEY (server_id) REFERENCES ssh_servers(id) ON DELETE CASCADE,
    FOREIGN KEY (agent_id) REFERENCES relay_agents(id) ON DELETE SET NULL,
    FOREIGN KEY (requested_by) REFERENCES users(id) ON DELETE SET NULL
);

-- 웹 터미널 한 번 = 한 행. 화면에 흐른 내용은 메모리에만 두고 DB 에 넣지
-- 않는다. 남기는 것은 "누가 언제 어느 서버에 붙어 무엇을 쳤는지" 다.
CREATE TABLE IF NOT EXISTS term_sessions (
    id           TEXT PRIMARY KEY,
    -- 서버를 목록에서 빼도 이 줄은 남는다. 누가 언제 어디에 붙어 무엇을
    -- 쳤는지는 그 서버가 사라진 뒤에도 남아야 한다. 그래서 NOT NULL 이
    -- 아니고 ON DELETE SET NULL 이다. (기록 화면은 '삭제된 서버' 로 보여 준다)
    server_id    INTEGER,
    user_id      INTEGER,
    state        TEXT NOT NULL DEFAULT 'opening'
                 CHECK (state IN ('opening', 'open', 'closed', 'failed')),
    opened_at    TEXT NOT NULL,
    closed_at    TEXT,
    last_io_at   TEXT,
    close_reason TEXT NOT NULL DEFAULT '',
    lines_in     INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (server_id) REFERENCES ssh_servers(id) ON DELETE SET NULL,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE SET NULL
);

-- 사람이 직접 친 줄. 등급을 매기지 않는다(사람의 계정 권한 그대로다).
-- 비밀번호를 묻는 프롬프트 뒤에 온 줄은 내용을 적지 않고 가린다.
CREATE TABLE IF NOT EXISTS term_inputs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    term_id    TEXT NOT NULL,
    line       TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (term_id) REFERENCES term_sessions(id) ON DELETE CASCADE
);

-- 챗봇이 스스로 고른 명령. 등급과 승인은 여기에만 걸린다.
CREATE TABLE IF NOT EXISTS ssh_commands (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    server_id   INTEGER,
    session_id  INTEGER,
    message_id  INTEGER,
    user_id     INTEGER,
    source      TEXT NOT NULL DEFAULT 'chat' CHECK (source IN ('chat', 'test')),
    command     TEXT NOT NULL,
    level       TEXT NOT NULL DEFAULT 'read' CHECK (level IN ('read', 'write', 'blocked')),
    state       TEXT NOT NULL DEFAULT 'pending'
                CHECK (state IN ('pending', 'running', 'done', 'failed',
                                 'rejected', 'expired', 'blocked', 'denied')),
    reason      TEXT NOT NULL DEFAULT '',
    approved_by INTEGER,
    approved_at TEXT,
    created_at  TEXT NOT NULL,
    finished_at TEXT,
    exit_code   INTEGER,
    duration_ms INTEGER,
    -- 결과의 "내용" 은 여기 넣지 않는다. 줄 수와 바이트 수 같은 요약만 넣는다.
    -- 명령 결과는 그 대화에만 남는다(messages). 기록은 관리자가 보는 자리이고,
    -- private 대화의 내용은 관리자도 볼 수 없어야 한다. 그 원칙이 여기서
    -- 뚫리면 "나만 보기" 가 거짓이 된다.
    result_note TEXT NOT NULL DEFAULT '',
    job_id      INTEGER,
    -- 전부 SET NULL 이다. 대화를 지운 사람이 그 대화에서 회사 서버에 나간
    -- 명령의 기록까지 지울 수 있으면 안 된다. 어느 대화였는지는 사라지지만
    -- 누가 언제 어느 서버에 무엇을 보냈는지는 남는다.
    FOREIGN KEY (server_id) REFERENCES ssh_servers(id) ON DELETE SET NULL,
    FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE SET NULL,
    FOREIGN KEY (message_id) REFERENCES messages(id) ON DELETE SET NULL,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE SET NULL
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
CREATE INDEX IF NOT EXISTS idx_user_menus_user    ON user_menus(user_id);
CREATE INDEX IF NOT EXISTS idx_user_patch_sites   ON user_patch_sites(user_id);
CREATE INDEX IF NOT EXISTS idx_patch_sites_root   ON patch_sites(root_id);
CREATE INDEX IF NOT EXISTS idx_patch_products_site ON patch_products(site_id);
CREATE INDEX IF NOT EXISTS idx_patch_modules_prod ON patch_modules(product_id);
CREATE INDEX IF NOT EXISTS idx_patch_files_product ON patch_files(product_id, date_dir);
CREATE INDEX IF NOT EXISTS idx_patch_files_module ON patch_files(module_id, version_sort);
CREATE INDEX IF NOT EXISTS idx_patch_scans_started ON patch_scans(started_at);
CREATE INDEX IF NOT EXISTS idx_ssh_grants_user    ON ssh_grants(user_id);
CREATE INDEX IF NOT EXISTS idx_relay_jobs_queue   ON relay_jobs(state, owner_id, id);
CREATE INDEX IF NOT EXISTS idx_relay_agents_owner ON relay_agents(owner_id, revoked_at);
CREATE INDEX IF NOT EXISTS idx_relay_jobs_term    ON relay_jobs(term_id, id);
CREATE INDEX IF NOT EXISTS idx_term_sessions_user ON term_sessions(user_id, state);
CREATE INDEX IF NOT EXISTS idx_term_inputs_term   ON term_inputs(term_id, id);
CREATE INDEX IF NOT EXISTS idx_ssh_cmd_created    ON ssh_commands(created_at);
CREATE INDEX IF NOT EXISTS idx_ssh_cmd_message    ON ssh_commands(message_id);
CREATE INDEX IF NOT EXISTS idx_ssh_cmd_session    ON ssh_commands(session_id, id);
CREATE INDEX IF NOT EXISTS idx_sessions_server    ON sessions(server_id);
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
def menu_check_outdated(conn):
    """user_menus 의 CHECK 제약이 'servers' 를 모르는 상태인지.

    sqlite 는 제약만 바꾸는 ALTER 가 없어서 표를 다시 만들어야 한다. 판단은
    sqlite_master 에 적힌 CREATE 문 원문을 보고 한다.
    """
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='user_menus'").fetchone()
    if row is None:
        return False
    return "'servers'" not in (row["sql"] or "")


def pending_migrations(conn):
    """적용해야 할 변경 목록을 사람이 읽을 수 있는 문자열로 돌려준다."""
    todo = []
    for t in ("users", "settings", "session_members", "audit_logs", "login_attempts",
              "notes", "note_attachments", "note_comments",
              # v6 : 메뉴 권한과 패치 저장소
              "user_menus", "user_patch_sites", "patch_version_rules", "patch_roots",
              "patch_sites", "patch_products", "patch_modules", "patch_files",
              "patch_scans",
              # v8 : SSH 중계
              "ssh_servers", "ssh_grants", "ssh_commands", "relay_agents",
              "relay_enroll_codes", "relay_jobs", "term_sessions", "term_inputs"):
        if not table_exists(conn, t):
            todo.append("CREATE TABLE %s" % t)
    # v8 : 기존 표에 더하는 컬럼과 CHECK 제약 변경
    if table_exists(conn, "sessions") and "server_id" not in column_names(conn, "sessions"):
        todo.append("sessions.server_id 추가")
    if table_exists(conn, "users"):
        ucols = column_names(conn, "users")
        if "ssh_all_servers" not in ucols:
            todo.append("users.ssh_all_servers 추가")
        if "ssh_level" not in ucols:
            todo.append("users.ssh_level 추가")
    if menu_check_outdated(conn):
        todo.append("user_menus 재작성 (서버 메뉴 키 허용)")
    # v9 : 중계를 사람마다 둔다
    if table_exists(conn, "relay_agents") and "owner_id" not in column_names(conn, "relay_agents"):
        todo.append("relay_agents.owner_id 추가")
    if table_exists(conn, "relay_jobs") and "owner_id" not in column_names(conn, "relay_jobs"):
        todo.append("relay_jobs.owner_id 추가")
    if table_exists(conn, "users") and "patch_all_sites" not in column_names(conn, "users"):
        todo.append("users.patch_all_sites 추가")
    # v7 : 모듈/파일 단위 공개 여부
    for t, c in (("patch_modules", "is_visible"), ("patch_files", "is_visible")):
        if table_exists(conn, t) and c not in column_names(conn, t):
            todo.append("%s.%s 추가" % (t, c))
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

        # v6 : 아래 두 가지는 "표를 지금 처음 만드는 경우"에만 채워야 한다.
        # 매 기동마다 다시 넣으면 관리자가 떼어 낸 메뉴 권한이 되살아나고,
        # 지운 내장 버전 규칙이 다시 생긴다. 그래서 creates 전에 미리 본다.
        had_user_menus = table_exists(conn, "user_menus")
        had_version_rules = table_exists(conn, "patch_version_rules")

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

            # --- v8 : user_menus 의 CHECK 제약에 'servers' 를 넣는다 ---------
            # 먼저 이름을 비켜 두면 아래 CREATE TABLE IF NOT EXISTS 가 새 제약으로
            # 만들어 준다. 그 다음에 옮겨 담고 헌 표를 버린다.
            rebuild_menus = menu_check_outdated(conn)
            if rebuild_menus:
                conn.execute("ALTER TABLE user_menus RENAME TO user_menus_old")

            for stmt in creates:
                conn.execute(stmt)

            if rebuild_menus:
                n = conn.execute(
                    "INSERT OR IGNORE INTO user_menus (user_id, menu_key, granted_at, granted_by)"
                    " SELECT user_id, menu_key, granted_at, granted_by FROM user_menus_old"
                    " WHERE menu_key IN ('chat', 'notes', 'patch', 'servers')").rowcount
                conn.execute("DROP TABLE user_menus_old")
                steps.append("user_menus 재작성, 기존 권한 %d건 그대로 옮김" % n)

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

            # --- v6 : 메뉴 권한과 패치 저장소 -----------------------------
            now = ts()

            # 1 = 모든 사이트, 0 = user_patch_sites 에 고른 것만.
            # 기본을 1 로 둬야 지금 동작이 그대로 유지된다.
            if "patch_all_sites" not in column_names(conn, "users"):
                conn.execute("ALTER TABLE users ADD COLUMN patch_all_sites "
                             "INTEGER NOT NULL DEFAULT 1")
                steps.append("users.patch_all_sites 추가 (기본 1 = 모든 사이트)")

            # --- v7 : 모듈/파일 단위 공개 여부 --------------------------
            # 기본 1 이어야 지금 보이는 것이 그대로 보인다. 0 으로 넣으면
            # 마이그레이션 직후 패치 목록이 통째로 비어 버린다.
            for t in ("patch_modules", "patch_files"):
                if "is_visible" not in column_names(conn, t):
                    conn.execute("ALTER TABLE %s ADD COLUMN is_visible "
                                 "INTEGER NOT NULL DEFAULT 1" % t)
                    steps.append("%s.is_visible 추가 (기본 1 = 보임)" % t)

            # --- v8 : SSH 중계 ------------------------------------------
            # 대화 하나에 서버 하나. 기존 대화는 붙은 서버가 없다(NULL).
            if "server_id" not in column_names(conn, "sessions"):
                conn.execute("ALTER TABLE sessions ADD COLUMN server_id INTEGER "
                             "REFERENCES ssh_servers(id)")
                steps.append("sessions.server_id 추가 (기존 대화는 붙은 서버 없음)")

            # 기본을 "꺼짐 / 고른 것만" 으로 둔다. 마이그레이션만으로 아무도
            # 서버에 붙을 수 있게 되면 안 된다. 관리자가 직접 켠다.
            ucols = column_names(conn, "users")
            if "ssh_all_servers" not in ucols:
                conn.execute("ALTER TABLE users ADD COLUMN ssh_all_servers "
                             "INTEGER NOT NULL DEFAULT 0")
                steps.append("users.ssh_all_servers 추가 (기본 0 = 고른 서버만)")
            if "ssh_level" not in ucols:
                conn.execute("ALTER TABLE users ADD COLUMN ssh_level TEXT NOT NULL "
                             "DEFAULT 'off'")
                steps.append("users.ssh_level 추가 (기본 off = 아무도 못 붙는다)")

            # --- v9 : 중계를 사람마다 둔다 --------------------------------
            # 사람마다 자기 VDI 에 중계를 깔고 자기 키로 붙는다. 그래서 일에도
            # 중계에도 "누구 것인지" 가 붙어야 한다. 기존 행(있다면)은 NULL 로
            # 남고, NULL 은 "주인 없는 중계" 라서 아무 일도 가져가지 못한다.
            if "owner_id" not in column_names(conn, "relay_agents"):
                conn.execute("ALTER TABLE relay_agents ADD COLUMN owner_id INTEGER "
                             "REFERENCES users(id)")
                # 주인이 없는 중계는 이제 아무 일도 받아 갈 수 없다(일마다 주인이
                # 붙는다). 그런데 목록에는 '붙어 있음' 으로 보인다. 할 수 없는
                # 일을 할 수 있는 것처럼 보여 주면 안 되므로 그 자리에서 끊는다.
                # 쓰던 사람은 등록 코드를 새로 받아 다시 등록하면 된다.
                n = conn.execute(
                    "UPDATE relay_agents SET revoked_at = ? WHERE revoked_at IS NULL",
                    (ts(),)).rowcount
                steps.append("relay_agents.owner_id 추가 (주인 없는 중계 %d대 해제)" % n)
            if "owner_id" not in column_names(conn, "relay_jobs"):
                conn.execute("ALTER TABLE relay_jobs ADD COLUMN owner_id INTEGER "
                             "REFERENCES users(id)")
                steps.append("relay_jobs.owner_id 추가")

            # 내장 버전 규칙. 표를 처음 만들 때만 넣는다.
            if not had_version_rules:
                conn.executemany(
                    "INSERT OR IGNORE INTO patch_version_rules"
                    " (name, pattern, sort_kind, sample, is_builtin, created_at, updated_at)"
                    " VALUES (?,?,?,?,1,?,?)",
                    [(r["name"], r["pattern"], r["sort_kind"], r["sample"], now, now)
                     for r in patch_rules.BUILTIN_VERSION_RULES])
                steps.append("내장 버전 규칙 %d건 등록"
                             % len(patch_rules.BUILTIN_VERSION_RULES))

            # 이 두 줄이 빠지면 마이그레이션 직후 전원이 아무 메뉴도 못 본다.
            # 지금 쓰고 있는 것(채팅/메모)을 그대로 넣어 준다. 패치는 아무에게도
            # 주지 않는다. 관리자가 직접 준다.
            if not had_user_menus:
                n = 0
                for key in ("chat", "notes"):
                    n += conn.execute(
                        "INSERT OR IGNORE INTO user_menus (user_id, menu_key, granted_at)"
                        " SELECT id, ?, ? FROM users", (key, now)).rowcount
                steps.append("기존 사용자에게 채팅/메모 메뉴 %d건 부여 (패치는 수동)" % n)

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
