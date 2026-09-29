#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
claude-web
==========

Project -> Session -> Messages / Attachments 구조의 단순한 Claude 웹 챗봇.

    브라우저 -> Flask -> claude -p -> Claude -> Flask -> 브라우저

- 로그인 없음. 모든 사용자가 같은 프로젝트/세션 목록을 공유한다.
- 대화 문맥은 Claude Code CLI 의 세션 기능으로 유지한다.
    첫 질문 : claude -p --session-id <uuid> ...
    이후    : claude -p --resume <uuid> ...
  (resume 실패 시 DB 의 최근 메시지를 프롬프트에 넣는 fallback 으로 자동 전환)
- 이미지는 서버에 저장한 뒤 절대경로를 프롬프트에 적어주고, Claude 가
  자체 Read 도구로 읽게 한다. (CLI 에 이미지 전용 옵션이 없음 / --add-dir 로 접근 허용)

설정은 환경변수 또는 .env 파일로 한다. (.env.example 참고)
"""

import json
import os
import shutil
import sqlite3
import subprocess
import threading
import time
import uuid

from flask import (
    Flask, abort, g, jsonify, render_template, request, send_file, session,
)
from werkzeug.exceptions import HTTPException

# ---------------------------------------------------------------------------
# .env 로드 (python-dotenv 가 없으면 조용히 건너뛴다)
# ---------------------------------------------------------------------------
try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # pragma: no cover
    pass

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def _env_int(name, default):
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _env_bool(name, default=True):
    v = os.getenv(name)
    if v is None:
        return default
    return v.strip().lower() not in ("0", "false", "no", "off", "")


# ---------------------------------------------------------------------------
# 설정
# ---------------------------------------------------------------------------
CLAUDE_BIN = os.getenv("CLAUDE_BIN", "claude")
CLAUDE_WORKDIR = os.getenv("CLAUDE_WORKDIR") or None
CLAUDE_TIMEOUT = _env_int("CLAUDE_TIMEOUT", 180)
CLAUDE_EXTRA_ARGS = os.getenv("CLAUDE_EXTRA_ARGS", "").split()
CLAUDE_USE_RESUME = _env_bool("CLAUDE_USE_RESUME", True)

DATABASE_PATH = os.getenv("DATABASE_PATH") or os.path.join(BASE_DIR, "data", "chat.db")
UPLOAD_DIR = os.getenv("UPLOAD_DIR") or os.path.join(BASE_DIR, "data", "uploads")
DATABASE_PATH = os.path.abspath(DATABASE_PATH)
UPLOAD_DIR = os.path.abspath(UPLOAD_DIR)

MAX_UPLOAD_MB = _env_int("MAX_UPLOAD_MB", 10)
MAX_IMAGES_PER_MESSAGE = _env_int("MAX_IMAGES_PER_MESSAGE", 5)
# 이전 이름(MAX_CONCURRENCY)도 계속 지원한다.
MAX_CONCURRENT_CLAUDE = _env_int("MAX_CONCURRENT_CLAUDE", _env_int("MAX_CONCURRENCY", 3))

HOST = os.getenv("HOST", "0.0.0.0")
PORT = _env_int("PORT", 8080)

MAX_INPUT_CHARS = _env_int("MAX_INPUT_CHARS", 8000)
# resume 를 못 쓸 때 프롬프트에 넣을 최근 대화 범위
MAX_HISTORY_MESSAGES = _env_int("MAX_HISTORY_MESSAGES", 16)
MAX_HISTORY_CHARS = _env_int("MAX_HISTORY_CHARS", 12000)

SECRET_KEY = os.getenv("SECRET_KEY") or os.urandom(32).hex()

# 업로드 허용 형식 : 확장자 + 매직바이트(내용) 둘 다 검사한다.
ALLOWED_IMAGES = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "webp": "image/webp",
    "gif": "image/gif",
}

os.makedirs(os.path.dirname(DATABASE_PATH), exist_ok=True)
os.makedirs(UPLOAD_DIR, exist_ok=True)

app = Flask(__name__)
app.config.update(
    SECRET_KEY=SECRET_KEY,
    # 요청 전체 한도. 개별 파일 한도는 MAX_UPLOAD_MB 로 따로 검사한다.
    MAX_CONTENT_LENGTH=MAX_UPLOAD_MB * max(MAX_IMAGES_PER_MESSAGE, 1) * 1024 * 1024 + 1024 * 1024,
    JSON_AS_ASCII=False,
)


# ---------------------------------------------------------------------------
# SQLite
# ---------------------------------------------------------------------------
SCHEMA = """
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
    name              TEXT NOT NULL,
    claude_session_id TEXT,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL,
    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS messages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL,
    role       TEXT NOT NULL CHECK (role IN ('user', 'assistant', 'error')),
    content    TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
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

CREATE INDEX IF NOT EXISTS idx_sessions_project ON sessions(project_id);
CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, id);
CREATE INDEX IF NOT EXISTS idx_attachments_message ON attachments(message_id);
CREATE INDEX IF NOT EXISTS idx_attachments_session ON attachments(session_id);
"""


def get_db():
    if "db" not in g:
        conn = sqlite3.connect(DATABASE_PATH, timeout=15.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 15000")
        g.db = conn
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    conn = sqlite3.connect(DATABASE_PATH, timeout=15.0)
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA foreign_keys = ON")
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()


def _ts():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def touch_session(db, session_id):
    now = _ts()
    db.execute("UPDATE sessions SET updated_at = ? WHERE id = ?", (now, session_id))
    db.execute(
        "UPDATE projects SET updated_at = ? "
        "WHERE id = (SELECT project_id FROM sessions WHERE id = ?)",
        (now, session_id),
    )


# ---------------------------------------------------------------------------
# 동시 실행 제어
#   - 전체 claude 프로세스 수 제한 (세마포어)
#   - 같은 세션에 동시 요청이 들어가면 문맥이 꼬이므로 세션 단위 lock (409)
# ---------------------------------------------------------------------------
_GLOBAL_SLOTS = threading.BoundedSemaphore(MAX_CONCURRENT_CLAUDE)
_SESSION_LOCKS = {}
_SESSION_LOCKS_GUARD = threading.Lock()


def session_lock(session_id):
    with _SESSION_LOCKS_GUARD:
        lock = _SESSION_LOCKS.get(session_id)
        if lock is None:
            lock = threading.Lock()
            _SESSION_LOCKS[session_id] = lock
        return lock


# ---------------------------------------------------------------------------
# Claude CLI 실행
# ---------------------------------------------------------------------------
def _claude_call(prompt, resume_id=None, new_session_id=None, extra_dirs=()):
    """
    claude -p 를 argument list 로 실행한다. (shell=True 미사용)
    반환: dict(ok, text, session_id, returncode)
    """
    # 주의: --add-dir 는 가변 인자(<directories...>)라서 바로 뒤에 플래그가 와야 한다.
    #       그렇지 않으면 마지막 프롬프트까지 디렉터리로 먹어버린다.
    args = [CLAUDE_BIN] + CLAUDE_EXTRA_ARGS + ["-p"]
    for d in extra_dirs:
        if d:
            args += ["--add-dir", d]
    args += ["--output-format", "json"]
    if resume_id:
        args += ["--resume", resume_id]
    elif new_session_id:
        args += ["--session-id", new_session_id]
    args.append(prompt)

    try:
        proc = subprocess.run(
            args,
            cwd=CLAUDE_WORKDIR,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=CLAUDE_TIMEOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except FileNotFoundError:
        return {"ok": False, "returncode": -1, "session_id": None, "text": (
            "Claude CLI 를 찾을 수 없습니다: %s\n"
            "`which claude` 로 경로를 확인한 뒤 CLAUDE_BIN 환경변수를 설정하세요." % CLAUDE_BIN)}
    except NotADirectoryError:
        return {"ok": False, "returncode": -1, "session_id": None,
                "text": "CLAUDE_WORKDIR 경로가 올바르지 않습니다: %s" % CLAUDE_WORKDIR}
    except PermissionError as exc:
        return {"ok": False, "returncode": -1, "session_id": None,
                "text": "Claude CLI 실행 권한이 없습니다: %s" % exc}
    except subprocess.TimeoutExpired:
        return {"ok": False, "returncode": -1, "session_id": None,
                "text": "시간 초과(%d초)되어 Claude 실행을 중단했습니다." % CLAUDE_TIMEOUT}
    except OSError as exc:
        return {"ok": False, "returncode": -1, "session_id": None,
                "text": "Claude 실행 중 오류가 발생했습니다: %s" % exc}

    stdout = (proc.stdout or "").strip()
    stderr = (proc.stderr or "").strip()

    if proc.returncode != 0:
        msg = "Claude 실행 실패 (exit code %d)" % proc.returncode
        if stderr:
            msg += "\n\n" + stderr
        elif stdout:
            msg += "\n\n" + stdout
        return {"ok": False, "returncode": proc.returncode, "session_id": None, "text": msg}

    # --output-format json 결과 파싱
    try:
        data = json.loads(stdout)
    except ValueError:
        if not stdout:
            return {"ok": False, "returncode": 0, "session_id": None,
                    "text": "Claude 가 빈 응답을 반환했습니다." + (("\n\n" + stderr) if stderr else "")}
        return {"ok": True, "returncode": 0, "session_id": None, "text": stdout}

    text = data.get("result")
    if not isinstance(text, str):
        text = json.dumps(data, ensure_ascii=False)[:2000]
    if data.get("is_error"):
        return {"ok": False, "returncode": 0, "session_id": data.get("session_id"),
                "text": text or "Claude 가 오류를 반환했습니다."}
    if not text.strip():
        return {"ok": False, "returncode": 0, "session_id": data.get("session_id"),
                "text": "Claude 가 빈 응답을 반환했습니다."}
    return {"ok": True, "returncode": 0, "session_id": data.get("session_id"), "text": text}


def build_prompt(question, images, history=None):
    """
    images  : [(original_name, absolute_path), ...]
    history : fallback 용 최근 메시지 목록 (resume 을 못 쓸 때만)
    """
    parts = []

    if history:
        lines = ["이전 대화:", ""]
        for m in history:
            lines.append("USER:" if m["role"] == "user" else "ASSISTANT:")
            lines.append(m["content"])
            lines.append("")
        parts.append("\n".join(lines))

    if images:
        lines = ["첨부된 이미지 파일 %d개:" % len(images)]
        for i, (name, path) in enumerate(images, 1):
            lines.append("%d. %s -> %s" % (i, name, path.replace("\\", "/")))
        lines.append("")
        lines.append("위 이미지 파일을 Read 도구로 열어서 확인한 뒤 답해줘.")
        parts.append("\n".join(lines))

    parts.append("사용자 질문:\n\n" + question)
    return "\n\n".join(parts)


def recent_history(db, session_id, exclude_message_id=None):
    rows = db.execute(
        "SELECT role, content FROM messages "
        "WHERE session_id = ? AND role != 'error' AND id != ? "
        "ORDER BY id DESC LIMIT ?",
        (session_id, exclude_message_id or -1, MAX_HISTORY_MESSAGES),
    ).fetchall()
    msgs = [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]
    total = sum(len(m["content"]) for m in msgs)
    while len(msgs) > 1 and total > MAX_HISTORY_CHARS:
        total -= len(msgs[0]["content"])
        msgs.pop(0)
    return msgs


def ask_claude(db, sess, question, images, user_message_id):
    """
    세션의 claude_session_id 를 이용해 문맥을 이어서 질문한다.
    반환: (ok, text, used_mode)
    """
    extra_dirs = [UPLOAD_DIR]
    claude_sid = sess["claude_session_id"]

    if CLAUDE_USE_RESUME and claude_sid:
        prompt = build_prompt(question, images)
        res = _claude_call(prompt, resume_id=claude_sid, extra_dirs=extra_dirs)
        if res["ok"]:
            return True, res["text"], "resume"
        app.logger.warning("resume 실패(session %s): %s", sess["id"],
                           res["text"].splitlines()[0] if res["text"] else "")
        # resume 실패 -> 새 claude 세션 + DB 기록으로 문맥 복원
        new_sid = str(uuid.uuid4())
        prompt = build_prompt(question, images,
                              history=recent_history(db, sess["id"], user_message_id))
        res = _claude_call(prompt, new_session_id=new_sid, extra_dirs=extra_dirs)
        if res["ok"]:
            db.execute("UPDATE sessions SET claude_session_id = ? WHERE id = ?",
                       (res["session_id"] or new_sid, sess["id"]))
            return True, res["text"], "fallback-new-session"
        return False, res["text"], "fallback-failed"

    if CLAUDE_USE_RESUME:
        # 이 세션의 첫 질문 : 우리가 만든 UUID 를 claude 세션 ID 로 지정한다.
        new_sid = str(uuid.uuid4())
        prompt = build_prompt(question, images)
        res = _claude_call(prompt, new_session_id=new_sid, extra_dirs=extra_dirs)
        if res["ok"]:
            db.execute("UPDATE sessions SET claude_session_id = ? WHERE id = ?",
                       (res["session_id"] or new_sid, sess["id"]))
            return True, res["text"], "new-session"
        return False, res["text"], "new-session-failed"

    # resume 기능을 끈 경우 : 항상 DB 기록을 프롬프트에 포함
    prompt = build_prompt(question, images,
                          history=recent_history(db, sess["id"], user_message_id))
    res = _claude_call(prompt, extra_dirs=extra_dirs)
    return res["ok"], res["text"], "history-prompt"


# ---------------------------------------------------------------------------
# 업로드 처리
# ---------------------------------------------------------------------------
_MAGIC = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
)


def sniff_mime(head):
    for magic, mime in _MAGIC:
        if head.startswith(magic):
            return mime
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    return None


def session_upload_dir(project_id, session_id):
    path = os.path.join(UPLOAD_DIR, "project_%d" % project_id, "session_%d" % session_id)
    os.makedirs(path, exist_ok=True)
    return path


def save_upload(storage, project_id, session_id):
    """
    검증 후 UUID 이름으로 저장한다.
    원본 파일명은 저장 경로 계산에 일절 쓰지 않으므로 path traversal 이 불가능하다.
    반환: dict 또는 (None, 오류메시지)
    """
    original = os.path.basename((storage.filename or "").replace("\\", "/")) or "image"
    ext = original.rsplit(".", 1)[-1].lower() if "." in original else ""
    if ext not in ALLOWED_IMAGES:
        return None, "허용되지 않는 확장자입니다: %s (허용: %s)" % (
            ext or "(없음)", ", ".join(sorted(ALLOWED_IMAGES)))

    storage.stream.seek(0, os.SEEK_END)
    size = storage.stream.tell()
    storage.stream.seek(0)
    if size <= 0:
        return None, "빈 파일입니다: %s" % original
    if size > MAX_UPLOAD_MB * 1024 * 1024:
        return None, "파일이 너무 큽니다: %s (%.1fMB / 최대 %dMB)" % (
            original, size / 1024.0 / 1024.0, MAX_UPLOAD_MB)

    head = storage.stream.read(32)
    storage.stream.seek(0)
    sniffed = sniff_mime(head)
    if sniffed is None:
        return None, "이미지 파일이 아닙니다: %s" % original
    if sniffed != ALLOWED_IMAGES[ext]:
        return None, "확장자와 실제 파일 내용이 다릅니다: %s (내용=%s)" % (original, sniffed)
    declared = (storage.mimetype or "").lower()
    if declared and declared != sniffed and declared != "application/octet-stream":
        return None, "MIME 타입이 올바르지 않습니다: %s (%s)" % (original, declared)

    stored_name = "%s.%s" % (uuid.uuid4().hex, ext)
    directory = session_upload_dir(project_id, session_id)
    full_path = os.path.join(directory, stored_name)
    storage.save(full_path)

    return {
        "original_name": original[:255],
        "stored_name": stored_name,
        "file_path": os.path.abspath(full_path),
        "mime_type": sniffed,
        "file_size": size,
    }, None


def remove_tree(path):
    """업로드 폴더 정리. 실패해도 예외를 밖으로 던지지 않는다."""
    if not path or not os.path.isdir(path):
        return True
    try:
        shutil.rmtree(path)
        return True
    except OSError as exc:
        app.logger.warning("업로드 폴더 삭제 실패 %s: %s", path, exc)
        return False


# ---------------------------------------------------------------------------
# 공통 헬퍼
# ---------------------------------------------------------------------------
def row_to_dict(row):
    return {k: row[k] for k in row.keys()}


def get_project_or_404(db, pid):
    row = db.execute("SELECT * FROM projects WHERE id = ?", (pid,)).fetchone()
    if row is None:
        abort(404, "프로젝트를 찾을 수 없습니다.")
    return row


def get_session_or_404(db, sid):
    row = db.execute("SELECT * FROM sessions WHERE id = ?", (sid,)).fetchone()
    if row is None:
        abort(404, "세션을 찾을 수 없습니다.")
    return row


def message_payload(db, session_id):
    msgs = db.execute(
        "SELECT * FROM messages WHERE session_id = ? ORDER BY id", (session_id,)
    ).fetchall()
    atts = db.execute(
        "SELECT id, message_id, original_name, mime_type, file_size "
        "FROM attachments WHERE session_id = ? ORDER BY id", (session_id,)
    ).fetchall()
    by_msg = {}
    for a in atts:
        by_msg.setdefault(a["message_id"], []).append(row_to_dict(a))

    out = []
    for m in msgs:
        d = row_to_dict(m)
        d["attachments"] = by_msg.get(m["id"], [])
        out.append(d)
    return out


@app.errorhandler(HTTPException)
def handle_http_error(exc):
    return jsonify(ok=False, error=exc.description), exc.code


@app.errorhandler(Exception)
def handle_error(exc):  # pragma: no cover
    app.logger.exception("unhandled error")
    return jsonify(ok=False, error="서버 내부 오류: %s" % exc), 500


# ---------------------------------------------------------------------------
# 화면 / 상태
# ---------------------------------------------------------------------------
@app.get("/")
def index():
    return render_template(
        "index.html",
        max_images=MAX_IMAGES_PER_MESSAGE,
        max_upload_mb=MAX_UPLOAD_MB,
        allowed_ext=sorted(ALLOWED_IMAGES),
    )


@app.get("/health")
def health():
    return jsonify(status="ok")


# --- PWA : 서비스워커는 루트 스코프에서 제공해야 전체 사이트를 제어할 수 있다 ---
@app.get("/sw.js")
def service_worker():
    resp = send_file(os.path.join(app.static_folder, "sw.js"),
                     mimetype="text/javascript")
    resp.headers["Service-Worker-Allowed"] = "/"
    resp.headers["Cache-Control"] = "no-cache"
    return resp


@app.get("/manifest.webmanifest")
def manifest():
    return send_file(os.path.join(app.static_folder, "manifest.webmanifest"),
                     mimetype="application/manifest+json")


# ---------------------------------------------------------------------------
# 프로젝트 API
# ---------------------------------------------------------------------------
@app.get("/api/projects")
def list_projects():
    db = get_db()
    rows = db.execute(
        "SELECT p.*, (SELECT COUNT(*) FROM sessions s WHERE s.project_id = p.id) AS session_count "
        "FROM projects p ORDER BY p.id"
    ).fetchall()
    return jsonify(ok=True, projects=[row_to_dict(r) for r in rows])


@app.post("/api/projects")
def create_project():
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    description = (data.get("description") or "").strip()
    if not name:
        abort(400, "프로젝트 이름을 입력해 주세요.")
    if len(name) > 200:
        abort(400, "프로젝트 이름이 너무 깁니다. (최대 200자)")

    db = get_db()
    now = _ts()
    cur = db.execute(
        "INSERT INTO projects (name, description, created_at, updated_at) VALUES (?,?,?,?)",
        (name, description[:2000], now, now),
    )
    db.commit()
    row = db.execute("SELECT * FROM projects WHERE id = ?", (cur.lastrowid,)).fetchone()
    return jsonify(ok=True, project=row_to_dict(row)), 201


@app.patch("/api/projects/<int:pid>")
def update_project(pid):
    data = request.get_json(silent=True) or {}
    db = get_db()
    get_project_or_404(db, pid)

    fields, values = [], []
    if "name" in data:
        name = (data.get("name") or "").strip()
        if not name:
            abort(400, "프로젝트 이름을 입력해 주세요.")
        fields.append("name = ?")
        values.append(name[:200])
    if "description" in data:
        fields.append("description = ?")
        values.append((data.get("description") or "").strip()[:2000])
    if not fields:
        abort(400, "변경할 내용이 없습니다.")

    fields.append("updated_at = ?")
    values += [_ts(), pid]
    db.execute("UPDATE projects SET %s WHERE id = ?" % ", ".join(fields), values)
    db.commit()
    row = db.execute("SELECT * FROM projects WHERE id = ?", (pid,)).fetchone()
    return jsonify(ok=True, project=row_to_dict(row))


@app.delete("/api/projects/<int:pid>")
def delete_project(pid):
    db = get_db()
    get_project_or_404(db, pid)

    # DB 먼저 정리(ON DELETE CASCADE) -> 커밋 후 파일 정리.
    # 파일 삭제가 실패해도 DB 트랜잭션에는 영향이 없다.
    db.execute("DELETE FROM projects WHERE id = ?", (pid,))
    db.commit()

    files_ok = remove_tree(os.path.join(UPLOAD_DIR, "project_%d" % pid))
    return jsonify(ok=True, deleted=pid, files_removed=files_ok)


# ---------------------------------------------------------------------------
# 세션 API
# ---------------------------------------------------------------------------
@app.get("/api/projects/<int:pid>/sessions")
def list_sessions(pid):
    db = get_db()
    get_project_or_404(db, pid)
    rows = db.execute(
        "SELECT s.*, (SELECT COUNT(*) FROM messages m WHERE m.session_id = s.id) AS message_count "
        "FROM sessions s WHERE s.project_id = ? ORDER BY s.id",
        (pid,),
    ).fetchall()
    return jsonify(ok=True, sessions=[row_to_dict(r) for r in rows])


@app.post("/api/projects/<int:pid>/sessions")
def create_session(pid):
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip() or "새 대화"
    db = get_db()
    get_project_or_404(db, pid)

    now = _ts()
    cur = db.execute(
        "INSERT INTO sessions (project_id, name, claude_session_id, created_at, updated_at) "
        "VALUES (?,?,NULL,?,?)",
        (pid, name[:200], now, now),
    )
    db.execute("UPDATE projects SET updated_at = ? WHERE id = ?", (now, pid))
    db.commit()
    row = db.execute("SELECT * FROM sessions WHERE id = ?", (cur.lastrowid,)).fetchone()
    return jsonify(ok=True, session=row_to_dict(row)), 201


@app.get("/api/sessions/<int:sid>")
def get_session(sid):
    db = get_db()
    row = get_session_or_404(db, sid)
    return jsonify(ok=True, session=row_to_dict(row))


@app.patch("/api/sessions/<int:sid>")
def update_session(sid):
    data = request.get_json(silent=True) or {}
    db = get_db()
    get_session_or_404(db, sid)

    fields, values = [], []
    if "name" in data:
        name = (data.get("name") or "").strip()
        if not name:
            abort(400, "세션 이름을 입력해 주세요.")
        fields.append("name = ?")
        values.append(name[:200])
    if data.get("reset_claude_session"):
        fields.append("claude_session_id = NULL")
    if not fields:
        abort(400, "변경할 내용이 없습니다.")

    fields.append("updated_at = ?")
    values += [_ts(), sid]
    db.execute("UPDATE sessions SET %s WHERE id = ?" % ", ".join(fields), values)
    db.commit()
    row = db.execute("SELECT * FROM sessions WHERE id = ?", (sid,)).fetchone()
    return jsonify(ok=True, session=row_to_dict(row))


@app.delete("/api/sessions/<int:sid>")
def delete_session(sid):
    db = get_db()
    row = get_session_or_404(db, sid)
    pid = row["project_id"]

    db.execute("DELETE FROM sessions WHERE id = ?", (sid,))
    db.execute("UPDATE projects SET updated_at = ? WHERE id = ?", (_ts(), pid))
    db.commit()

    files_ok = remove_tree(os.path.join(UPLOAD_DIR, "project_%d" % pid, "session_%d" % sid))
    with _SESSION_LOCKS_GUARD:
        _SESSION_LOCKS.pop(sid, None)
    return jsonify(ok=True, deleted=sid, files_removed=files_ok)


# ---------------------------------------------------------------------------
# 메시지 API
# ---------------------------------------------------------------------------
@app.get("/api/sessions/<int:sid>/messages")
def list_messages(sid):
    db = get_db()
    get_session_or_404(db, sid)
    return jsonify(ok=True, messages=message_payload(db, sid))


@app.post("/api/sessions/<int:sid>/messages")
def post_message(sid):
    """
    multipart/form-data : message=텍스트, images=파일(복수)
    application/json    : {"message": "..."}
    """
    db = get_db()
    sess = get_session_or_404(db, sid)

    if request.files or request.form:
        # multipart/form-data 또는 application/x-www-form-urlencoded
        text = request.form.get("message", "")
        files = [f for f in request.files.getlist("images") if f and f.filename]
    else:
        data = request.get_json(silent=True) or {}
        text = data.get("message", "")
        files = []

    if not isinstance(text, str):
        abort(400, "잘못된 요청 형식입니다.")
    text = text.replace("\r\n", "\n").strip()
    if not text and not files:
        abort(400, "질문을 입력해 주세요.")
    if len(text) > MAX_INPUT_CHARS:
        abort(413, "입력이 너무 깁니다. (%d자 / 최대 %d자)" % (len(text), MAX_INPUT_CHARS))
    if len(files) > MAX_IMAGES_PER_MESSAGE:
        abort(400, "이미지는 한 번에 최대 %d개까지 첨부할 수 있습니다. (요청 %d개)"
              % (MAX_IMAGES_PER_MESSAGE, len(files)))

    # --- 업로드 검증/저장 (DB 기록 전에 끝낸다) -----------------------------
    saved = []
    for f in files:
        info, err = save_upload(f, sess["project_id"], sid)
        if err:
            for s in saved:  # 이미 저장한 파일 되돌리기
                try:
                    os.remove(s["file_path"])
                except OSError:
                    pass
            abort(400, err)
        saved.append(info)

    if not text:
        text = "첨부한 이미지를 확인해줘."

    # --- 세션 단위 lock : 같은 세션 동시 요청 차단 --------------------------
    lock = session_lock(sid)
    if not lock.acquire(blocking=False):
        for s in saved:
            try:
                os.remove(s["file_path"])
            except OSError:
                pass
        abort(409, "이 세션은 현재 Claude 응답을 처리 중입니다. 잠시 후 다시 시도해 주세요.")

    try:
        if not _GLOBAL_SLOTS.acquire(blocking=False):
            for s in saved:
                try:
                    os.remove(s["file_path"])
                except OSError:
                    pass
            abort(429, "서버가 처리 중인 요청이 많습니다(최대 %d개). 잠시 후 다시 시도해 주세요."
                  % MAX_CONCURRENT_CLAUDE)
        try:
            now = _ts()
            cur = db.execute(
                "INSERT INTO messages (session_id, role, content, created_at) VALUES (?,?,?,?)",
                (sid, "user", text, now),
            )
            user_msg_id = cur.lastrowid
            for s in saved:
                db.execute(
                    "INSERT INTO attachments (session_id, message_id, original_name, stored_name,"
                    " file_path, mime_type, file_size, created_at) VALUES (?,?,?,?,?,?,?,?)",
                    (sid, user_msg_id, s["original_name"], s["stored_name"], s["file_path"],
                     s["mime_type"], s["file_size"], now),
                )
            touch_session(db, sid)
            db.commit()

            started = time.time()
            images = [(s["original_name"], s["file_path"]) for s in saved]
            ok, reply, mode = ask_claude(db, sess, text, images, user_msg_id)
            elapsed = round(time.time() - started, 2)

            db.execute(
                "INSERT INTO messages (session_id, role, content, created_at) VALUES (?,?,?,?)",
                (sid, "assistant" if ok else "error", reply, _ts()),
            )
            touch_session(db, sid)
            db.commit()
        finally:
            _GLOBAL_SLOTS.release()
    finally:
        lock.release()

    if not ok:
        app.logger.warning("claude 실패 (session %s, %s): %s", sid, mode,
                           reply.splitlines()[0] if reply else "")

    messages = message_payload(db, sid)
    return jsonify(ok=ok, mode=mode, elapsed=elapsed,
                   messages=messages[-2:], error=None if ok else reply)


# ---------------------------------------------------------------------------
# 첨부파일 API : DB 의 id 로만 조회한다 (파일 경로를 URL 로 받지 않음)
# ---------------------------------------------------------------------------
@app.get("/api/attachments/<int:aid>")
def get_attachment(aid):
    db = get_db()
    row = db.execute("SELECT * FROM attachments WHERE id = ?", (aid,)).fetchone()
    if row is None:
        abort(404, "첨부파일을 찾을 수 없습니다.")

    path = os.path.realpath(row["file_path"])
    root = os.path.realpath(UPLOAD_DIR)
    # 업로드 디렉터리 밖 경로는 무조건 거부 (드라이브가 다르면 ValueError)
    try:
        inside = os.path.commonpath([path, root]) == root
    except ValueError:
        inside = False
    if not inside:
        abort(403, "허용되지 않는 경로입니다.")
    if not os.path.isfile(path):
        abort(404, "파일이 존재하지 않습니다.")

    return send_file(path, mimetype=row["mime_type"],
                     download_name=row["original_name"], as_attachment=False)


# ---------------------------------------------------------------------------
# 구버전 호환 API (단순 1세션 채팅)
# ---------------------------------------------------------------------------
def _legacy_session(db):
    row = db.execute("SELECT * FROM projects WHERE name = ?", ("기본",)).fetchone()
    if row is None:
        now = _ts()
        cur = db.execute(
            "INSERT INTO projects (name, description, created_at, updated_at) VALUES (?,?,?,?)",
            ("기본", "구버전 /chat API 용", now, now))
        pid = cur.lastrowid
    else:
        pid = row["id"]

    sid = session.get("legacy_sid")
    if sid:
        s = db.execute("SELECT * FROM sessions WHERE id = ?", (sid,)).fetchone()
        if s is not None:
            return s
    now = _ts()
    cur = db.execute(
        "INSERT INTO sessions (project_id, name, claude_session_id, created_at, updated_at) "
        "VALUES (?,?,NULL,?,?)", (pid, "기본 대화", now, now))
    db.commit()
    session["legacy_sid"] = cur.lastrowid
    return db.execute("SELECT * FROM sessions WHERE id = ?", (cur.lastrowid,)).fetchone()


@app.post("/chat")
def legacy_chat():
    db = get_db()
    sess = _legacy_session(db)
    data = request.get_json(silent=True) or {}
    message = (data.get("message") or "").strip()
    if not message:
        return jsonify(ok=False, error="질문을 입력해 주세요."), 400

    lock = session_lock(sess["id"])
    if not lock.acquire(blocking=False):
        return jsonify(ok=False, error="이 세션은 처리 중입니다."), 409
    try:
        if not _GLOBAL_SLOTS.acquire(blocking=False):
            return jsonify(ok=False, error="서버가 처리 중인 요청이 많습니다."), 429
        try:
            now = _ts()
            cur = db.execute(
                "INSERT INTO messages (session_id, role, content, created_at) VALUES (?,?,?,?)",
                (sess["id"], "user", message, now))
            started = time.time()
            ok, reply, _mode = ask_claude(db, sess, message, [], cur.lastrowid)
            db.execute(
                "INSERT INTO messages (session_id, role, content, created_at) VALUES (?,?,?,?)",
                (sess["id"], "assistant" if ok else "error", reply, _ts()))
            touch_session(db, sess["id"])
            db.commit()
        finally:
            _GLOBAL_SLOTS.release()
    finally:
        lock.release()

    elapsed = round(time.time() - started, 2)
    if ok:
        return jsonify(ok=True, reply=reply, elapsed=elapsed)
    return jsonify(ok=False, error=reply, elapsed=elapsed), 500


@app.route("/clear", methods=["POST", "GET"])
def legacy_clear():
    sid = session.pop("legacy_sid", None)
    if sid:
        db = get_db()
        db.execute("DELETE FROM sessions WHERE id = ?", (sid,))
        db.commit()
    return jsonify(ok=True, status="cleared")


init_db()

if __name__ == "__main__":
    app.logger.warning(
        "claude-web starting: bin=%s workdir=%s db=%s uploads=%s timeout=%ss concurrency=%s port=%s",
        CLAUDE_BIN, CLAUDE_WORKDIR or os.getcwd(), DATABASE_PATH, UPLOAD_DIR,
        CLAUDE_TIMEOUT, MAX_CONCURRENT_CLAUDE, PORT,
    )
    app.run(host=HOST, port=PORT, threaded=True, debug=False)
