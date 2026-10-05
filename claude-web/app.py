#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
claude-web
==========

사내 공용 chat-bot 포털.

    브라우저 -> 로그인 -> Flask -> Claude Provider -> Claude -> 브라우저

구성
----
    config.py          .env 기반 고정 설정 (경로/포트/키)
    db.py              SQLite 스키마 + 마이그레이션
    settings_store.py  운영 설정(Claude 연결)을 DB 에서 관리
    providers.py       Claude 호출 추상화 (CLI / API)
    auth.py            로그인 / CSRF / rate limit / 최초 관리자
    permissions.py     private/public 권한 규칙
    admin.py           관리자 페이지 + 관리 API
    notes.py           개인/공유 메모 (화면 + API)
    app.py             화면과 채팅 API (이 파일)

Project = 그룹핑(관리자가 관리), Session = 접근 권한 단위(소유자가 관리).

관리 명령
---------
    python app.py                 서버 실행
    python app.py create-admin    관리자 계정 생성 (대화형)
    python app.py migrate         스키마 마이그레이션만 수행
    python app.py backup          DB 백업본 생성
"""

import getpass
import mimetypes
import os
import shutil
import sys
import threading
import time
import uuid

from flask import (
    Flask, abort, jsonify, redirect, render_template, request, send_file, url_for,
)
from werkzeug.exceptions import HTTPException

import admin as admin_module
import admin_patch as admin_patch_module
import admin_relay as admin_relay_module
import auth
import config
import notes as notes_module
import patch as patch_module
import patch_scan
import permissions
import providers
import relay as relay_module
import relay_store
import settings_store
from db import (
    audit, backup_database, close_db, connect, get_db, migrate, row_to_dict, ts,
)

# woff2 는 파이썬 표준 mimetypes 표에 없다. 리눅스에서는 /etc/mime.types 가
# 채워 주지만 python:slim 계열 이미지에는 그 파일이 없어서 글꼴이
# application/octet-stream 으로 나간다. 직접 등록해 둔다.
mimetypes.add_type("font/woff2", ".woff2")

BASE_DIR = config.BASE_DIR
UPLOAD_DIR = config.UPLOAD_DIR

os.makedirs(os.path.dirname(config.DATABASE_PATH), exist_ok=True)
os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(config.NOTES_DIR, exist_ok=True)

# 요청 본문 한도는 Flask 전역 설정이라 채팅 첨부와 메모 첨부 중 **큰 쪽**에 맞춘다.
# 각 라우트는 자기 기준(개수/용량)으로 다시 검사하므로 이 값이 크다고 해서
# 채팅에 10MB 넘는 이미지가 들어오지는 않는다.
# 기본값에서는 둘이 같아(10MB x 5 + 1MB = 51MiB) nginx 의 client_max_body_size(52m)를
# 바꿀 필요가 없다. 한쪽을 올리면 nginx 설정도 함께 올려야 한다.
_MAX_BODY = max(
    config.MAX_UPLOAD_MB * max(config.MAX_IMAGES_PER_MESSAGE, 1),
    config.MAX_NOTE_ATTACHMENT_MB * max(config.MAX_NOTE_ATTACHMENTS, 1),
) * 1024 * 1024 + 1024 * 1024

app = Flask(__name__)
app.config.update(
    SECRET_KEY=config.SECRET_KEY,
    MAX_CONTENT_LENGTH=_MAX_BODY,
    JSON_AS_ASCII=False,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=config.SESSION_COOKIE_SECURE,
    PERMANENT_SESSION_LIFETIME=config.SESSION_LIFETIME_DAYS * 86400,
)

app.teardown_appcontext(close_db)
app.register_blueprint(auth.bp)
app.register_blueprint(admin_module.bp)
app.register_blueprint(admin_module.api)
app.register_blueprint(notes_module.bp)
app.register_blueprint(notes_module.api)
app.register_blueprint(patch_module.bp)
app.register_blueprint(patch_module.api)
app.register_blueprint(admin_patch_module.bp)
app.register_blueprint(admin_patch_module.api)
app.register_blueprint(relay_module.bp)
app.register_blueprint(relay_module.api)
app.register_blueprint(relay_module.relay_api)
app.register_blueprint(admin_relay_module.bp)
app.register_blueprint(admin_relay_module.api)


# ---------------------------------------------------------------------------
# 동시 실행 제어
#   - 전체 동시 Claude 요청 수 제한 (관리자 설정값. 재시작 없이 즉시 반영)
#   - 같은 세션 동시 요청은 문맥이 꼬이므로 세션 단위 lock (409)
#     public 세션은 여러 사람이 함께 쓰므로 이 lock 이 특히 중요하다.
# ---------------------------------------------------------------------------
class ConcurrencyLimiter(object):
    def __init__(self):
        self._lock = threading.Lock()
        self._active = 0

    def acquire(self, limit):
        with self._lock:
            if self._active >= limit:
                return False
            self._active += 1
            return True

    def release(self):
        with self._lock:
            self._active = max(0, self._active - 1)

    def active(self):
        with self._lock:
            return self._active


_LIMITER = ConcurrencyLimiter()
_SESSION_LOCKS = {}
_SESSION_LOCKS_GUARD = threading.Lock()


def session_lock(session_id):
    with _SESSION_LOCKS_GUARD:
        lock = _SESSION_LOCKS.get(session_id)
        if lock is None:
            lock = threading.Lock()
            _SESSION_LOCKS[session_id] = lock
        return lock


def get_provider(db):
    cfg = settings_store.snapshot(db)
    cfg["upload_dir"] = UPLOAD_DIR
    return providers.build(cfg)


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


def rel_upload_path(full_path):
    """
    DB 에 넣을 값을 만든다. UPLOAD_DIR 기준 **상대경로**다.

    절대경로를 넣으면 개발 PC 에서 만든 DB 를 운영 서버로 옮겼을 때
    (C:\\...\\data\\uploads -> /var/lib/claude-web/uploads) 모든 첨부가
    경로 검사에 걸려 열리지 않는다. 구분자는 항상 "/" 로 저장해 OS 도 타지 않는다.
    """
    return os.path.relpath(os.path.abspath(full_path), UPLOAD_DIR).replace("\\", "/")


def abs_upload_path(stored):
    """DB 의 file_path 를 실제 경로로 되돌린다. 마이그레이션 전 절대경로도 받는다."""
    value = str(stored or "")
    if value.startswith(("/", "\\")) or (len(value) > 2 and value[1] == ":"):
        return value
    return os.path.join(UPLOAD_DIR, value.replace("/", os.sep))


def save_upload(storage, project_id, session_id):
    """
    확장자 + 매직바이트 + 선언 MIME 세 가지를 모두 검사하고 UUID 이름으로 저장한다.
    사용자가 준 원본 파일명은 저장 경로 계산에 일절 쓰지 않으므로
    `../../etc/passwd` 같은 이름이 와도 경로가 바뀌지 않는다.
    """
    original = os.path.basename((storage.filename or "").replace("\\", "/")) or "image"
    ext = original.rsplit(".", 1)[-1].lower() if "." in original else ""
    if ext not in config.ALLOWED_IMAGES:
        return None, "허용되지 않는 확장자입니다: %s (허용: %s)" % (
            ext or "(없음)", ", ".join(sorted(config.ALLOWED_IMAGES)))

    storage.stream.seek(0, os.SEEK_END)
    size = storage.stream.tell()
    storage.stream.seek(0)
    if size <= 0:
        return None, "빈 파일입니다: %s" % original
    if size > config.MAX_UPLOAD_MB * 1024 * 1024:
        return None, "파일이 너무 큽니다: %s (%.1fMB / 최대 %dMB)" % (
            original, size / 1024.0 / 1024.0, config.MAX_UPLOAD_MB)

    head = storage.stream.read(32)
    storage.stream.seek(0)
    sniffed = sniff_mime(head)
    if sniffed is None:
        return None, "이미지 파일이 아닙니다: %s" % original
    if sniffed != config.ALLOWED_IMAGES[ext]:
        return None, "확장자와 실제 파일 내용이 다릅니다: %s (내용=%s)" % (original, sniffed)
    declared = (storage.mimetype or "").lower()
    if declared and declared != sniffed and declared != "application/octet-stream":
        return None, "MIME 타입이 올바르지 않습니다: %s (%s)" % (original, declared)

    stored_name = "%s.%s" % (uuid.uuid4().hex, ext)
    full_path = os.path.join(session_upload_dir(project_id, session_id), stored_name)
    storage.save(full_path)
    return {
        "original_name": original[:255],
        "stored_name": stored_name,
        "file_path": rel_upload_path(full_path),   # DB 에 들어가는 값
        "abs_path": os.path.abspath(full_path),    # 이번 요청에서만 쓰는 실제 경로
        "mime_type": sniffed,
        "file_size": size,
    }, None


def remove_tree(path):
    """업로드 폴더 정리. 실패해도 예외를 밖으로 던지지 않는다. (DB 는 이미 커밋됨)"""
    if not path or not os.path.isdir(path):
        return True
    try:
        shutil.rmtree(path)
        return True
    except OSError as exc:
        app.logger.warning("업로드 폴더 삭제 실패 %s: %s", path, exc)
        return False


def discard_uploads(saved):
    for s in saved:
        try:
            os.remove(s["abs_path"])
        except OSError:
            pass


# ---------------------------------------------------------------------------
# 대화
# ---------------------------------------------------------------------------
def touch_session(db, session_id):
    now = ts()
    db.execute("UPDATE sessions SET updated_at = ? WHERE id = ?", (now, session_id))
    db.execute(
        "UPDATE projects SET updated_at = ? "
        "WHERE id = (SELECT project_id FROM sessions WHERE id = ?)",
        (now, session_id))


def recent_history(db, session_id, exclude_message_id=None, with_author=False):
    """provider 가 resume 을 못 쓸 때 문맥으로 넣을 최근 대화."""
    rows = db.execute(
        "SELECT m.role, m.content, u.display_name, u.username "
        "FROM messages m LEFT JOIN users u ON u.id = m.user_id "
        "WHERE m.session_id = ? AND m.role != 'error' AND m.id != ? "
        "ORDER BY m.id DESC LIMIT ?",
        (session_id, exclude_message_id or -1, config.MAX_HISTORY_MESSAGES)).fetchall()

    msgs = []
    for r in reversed(rows):
        content = r["content"]
        if with_author and r["role"] == "user":
            who = r["display_name"] or r["username"]
            if who:
                content = "%s: %s" % (who, content)
        msgs.append({"role": r["role"], "content": content})

    total = sum(len(m["content"]) for m in msgs)
    while len(msgs) > 1 and total > config.MAX_HISTORY_CHARS:
        total -= len(msgs[0]["content"])
        msgs.pop(0)
    return msgs


def ask_claude(db, provider, sess, question, images, user_message_id, author=None):
    """
    provider 에 질문을 넘기고 (ok, text, mode) 를 돌려준다.

    provider 가 세션 resume 을 지원하면 sessions.claude_session_id 로 문맥을 잇고,
    지원하지 않으면(예: API provider) DB 의 최근 대화를 history 로 넘긴다.
    어느 쪽이든 호출부는 동일하다.
    """
    is_public = sess["visibility"] == permissions.PUBLIC
    # 공개 세션은 여러 사람이 함께 쓰므로 누가 한 말인지 알려준다.
    # (DB 에는 원문 그대로 저장하고, Claude 에 넘길 때만 붙인다)
    asked = "%s: %s" % (author, question) if (is_public and author) else question

    if provider.supports_resume():
        claude_sid = sess["claude_session_id"]
        if claude_sid:
            res = provider.send(asked, images=images, resume_id=claude_sid)
            if res["ok"]:
                return True, res["text"], "resume"
            app.logger.warning("resume 실패(session %s): %s", sess["id"],
                               (res["text"] or "").splitlines()[0])
            new_sid = str(uuid.uuid4())
            history = recent_history(db, sess["id"], user_message_id, with_author=is_public)
            res = provider.send(asked, images=images, history=history,
                                new_session_id=new_sid)
            if res["ok"]:
                db.execute("UPDATE sessions SET claude_session_id = ? WHERE id = ?",
                           (res["session_id"] or new_sid, sess["id"]))
                return True, res["text"], "fallback-new-session"
            return False, res["text"], "fallback-failed"

        new_sid = str(uuid.uuid4())
        res = provider.send(asked, images=images, new_session_id=new_sid)
        if res["ok"]:
            db.execute("UPDATE sessions SET claude_session_id = ? WHERE id = ?",
                       (res["session_id"] or new_sid, sess["id"]))
            return True, res["text"], "new-session"
        return False, res["text"], "new-session-failed"

    history = recent_history(db, sess["id"], user_message_id, with_author=is_public)
    res = provider.send(asked, images=images, history=history)
    return res["ok"], res["text"], "history-prompt"


# ---------------------------------------------------------------------------
# 공통 조회 / 직렬화
# ---------------------------------------------------------------------------
def get_project_or_404(db, pid):
    row = db.execute("SELECT * FROM projects WHERE id = ?", (pid,)).fetchone()
    if row is None:
        abort(404, "프로젝트를 찾을 수 없습니다.")
    return row


def get_session_row(db, sid):
    return db.execute(
        "SELECT s.*, u.username AS owner_username, u.display_name AS owner_display_name "
        "FROM sessions s LEFT JOIN users u ON u.id = s.owner_id WHERE s.id = ?",
        (sid,)).fetchone()


def get_session_or_404(db, sid):
    row = get_session_row(db, sid)
    if row is None:
        abort(404, "세션을 찾을 수 없습니다.")
    return row


def session_payload(db, user, row):
    d = row_to_dict(row)
    d.pop("claude_session_id", None)  # 내부 식별자는 밖으로 내보내지 않는다
    keys = row.keys()
    d["owner_name"] = (d.pop("owner_display_name", None) or d.pop("owner_username", None)
                       if "owner_display_name" in keys else None)
    d.pop("owner_display_name", None)
    d.pop("owner_username", None)
    d["is_owner"] = permissions.is_owner(user, row)
    d["can_manage"] = permissions.can_manage_session(user, row)
    d["can_write"] = permissions.can_write_session(db, user, row)
    d["is_legacy"] = row["owner_id"] is None

    # 대화 하나에 서버 하나. 붙은 서버가 없으면 server 는 None 이다.
    # 서버가 지워졌거나 그 사람의 허용 범위에서 빠지면 이름은 보여 주되
    # usable 을 false 로 내려 "고를 수는 없다" 를 화면이 알 수 있게 한다.
    d["server"] = None
    if "server_id" in keys and row["server_id"]:
        srv = relay_store.get_server(db, row["server_id"])
        if srv is not None:
            d["server"] = {
                "id": srv["id"], "name": srv["name"], "host": srv["host"],
                "username": srv["username"],
                "is_enabled": bool(srv["is_enabled"]),
                "usable": (bool(srv["is_enabled"])
                           and permissions.can_use_server(db, user, srv)),
            }
    return d


def message_payload(db, session_id):
    msgs = db.execute(
        "SELECT m.*, u.username AS author_username, u.display_name AS author_display_name "
        "FROM messages m LEFT JOIN users u ON u.id = m.user_id "
        "WHERE m.session_id = ? ORDER BY m.id", (session_id,)).fetchall()
    atts = db.execute(
        "SELECT id, message_id, original_name, mime_type, file_size "
        "FROM attachments WHERE session_id = ? ORDER BY id", (session_id,)).fetchall()
    by_msg = {}
    for a in atts:
        by_msg.setdefault(a["message_id"], []).append(row_to_dict(a))

    # 승인 카드. 챗봇이 고른 명령은 그 답 메시지에 붙어 있다.
    cmds = relay_store.commands_for_messages(db, [m["id"] for m in msgs])

    out = []
    for m in msgs:
        d = row_to_dict(m)
        display = d.pop("author_display_name", None)
        username = d.pop("author_username", None)
        d["author_name"] = display or username
        d["attachments"] = by_msg.get(m["id"], [])
        d["commands"] = cmds.get(m["id"], [])
        out.append(d)
    return out


# ---------------------------------------------------------------------------
# 요청 훅
# ---------------------------------------------------------------------------
PUBLIC_PATHS = ("/login", "/setup", "/health", "/sw.js", "/manifest.webmanifest")


@app.before_request
def before():
    auth.load_current_user()
    if request.path.startswith("/static/") or request.path in ("/health",):
        return None
    # 중계 프로그램의 API 는 쿠키를 쓰지 않고 X-Relay-Key 헤더로만 인증한다.
    # 브라우저의 form 은 그 헤더를 붙일 수 없으므로 CSRF 로 공격할 대상이
    # 아니고, 반대로 CSRF 토큰을 요구하면 중계가 로그인을 해야 한다.
    if request.path.startswith(relay_module.NO_CSRF_PREFIX):
        return None
    auth.check_csrf()
    return None


@app.after_request
def after(resp):
    # 인증된 화면/API 응답이 중간 캐시나 서비스워커에 남지 않게 한다.
    # 앱 셸("/") 에도 로그인한 사용자 이름과 CSRF 토큰이 들어가므로 포함해야 한다.
    # 로그인과 무관한 정적 리소스와 PWA 파일만 캐시를 허용한다.
    if not request.path.startswith(("/static/", "/manifest.webmanifest", "/sw.js")):
        resp.headers.setdefault("Cache-Control", "no-store, no-cache, must-revalidate")
        resp.headers.setdefault("Pragma", "no-cache")
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    resp.headers.setdefault("Referrer-Policy", "same-origin")
    return resp


# 오류 화면의 큰 제목. 상태 코드 숫자만 크게 띄우지 않고 무슨 일이 생겼는지
# 한 문장으로 적는다. 사과하지 않고 다음에 할 일만 말한다.
ERROR_TITLES = {
    400: "요청을 이해하지 못했습니다",
    403: "볼 수 있는 권한이 없습니다",
    404: "찾을 수 없는 주소입니다",
    405: "이 방법으로는 열 수 없습니다",
    413: "보낸 파일이 너무 큽니다",
    429: "요청이 너무 잦습니다",
    500: "서버에 문제가 생겼습니다",
    502: "서버에 연결하지 못했습니다",
    503: "지금은 서비스를 쓸 수 없습니다",
}


def error_title(code):
    return ERROR_TITLES.get(code, "요청을 처리하지 못했습니다")


@app.errorhandler(HTTPException)
def handle_http_error(exc):
    if auth.wants_json() or request.path.startswith("/api/"):
        return jsonify(ok=False, error=exc.description), exc.code
    if exc.code == 401:
        return redirect(url_for("auth.login", next=request.path))
    # abort(403, "직접 쓴 설명") 처럼 우리가 넣은 문장만 화면에 보여 준다.
    # 넣지 않았다면 exc.description 에는 werkzeug 기본 영어 문장이 들어 있어서,
    # 그대로 두면 한국어 화면 한가운데에 영어 안내가 크게 뜬다.
    detail = exc.description
    if detail == getattr(type(exc), "description", None):
        detail = ""
    return render_template("error.html", code=exc.code,
                           title=error_title(exc.code), message=detail), exc.code


@app.errorhandler(Exception)
def handle_error(exc):  # pragma: no cover
    app.logger.exception("unhandled error")
    if auth.wants_json() or request.path.startswith("/api/"):
        return jsonify(ok=False, error="서버 내부 오류가 발생했습니다."), 500
    return render_template("error.html", code=500, title=error_title(500),
                           message=""), 500


# ---------------------------------------------------------------------------
# 화면
# ---------------------------------------------------------------------------
@app.get("/")
@auth.login_required
def index():
    """
    채팅 화면. 채팅 메뉴가 없는 사람은 여기서 막히면 안 된다.

    "/" 는 로그인 직후 모두가 거쳐 가는 자리다. 여기서 403 을 내면 메모만
    가진 사람은 로그인하자마자 아무것도 못 하게 된다. 그래서 막는 대신
    그 사람이 가진 메뉴 중 첫 번째로 보낸다. 하나도 없으면 안내 화면이다.
    """
    user = auth.current_user()
    db = get_db()
    menus = permissions.user_menus(db, user)
    if "chat" not in menus:
        dest = permissions.landing_path(db, user)
        return redirect(dest if dest else url_for("no_access"))
    return render_template(
        "index.html",
        csrf=auth.csrf_token(),
        me=auth.public_user(user),
        is_admin=permissions.is_admin(user),
        menus=sorted(menus),
        # 왼쪽 레일용. rail_menu 는 이 화면에만 있는 아바타 메뉴를 그리라는 뜻이다.
        rail="chat",
        rail_menu=True,
        max_images=config.MAX_IMAGES_PER_MESSAGE,
        max_upload_mb=config.MAX_UPLOAD_MB,
        allowed_ext=sorted(config.ALLOWED_IMAGES),
    )


@app.get("/health")
def health():
    return jsonify(status="ok")


@app.get("/no-access")
@auth.login_required
def no_access():
    """
    메뉴를 하나도 못 받은 사람이 보는 화면.

    빈 화면이나 403 대신 무엇을 하면 되는지 한 줄로 알려 주고, 로그아웃
    단추를 반드시 둔다. 들어왔는데 나갈 길이 없는 화면을 만들면 안 된다.
    """
    user = auth.current_user()
    db = get_db()
    dest = permissions.landing_path(db, user)
    if dest:                      # 그 사이에 권한을 받았으면 바로 보낸다
        return redirect(dest)
    return render_template("no_access.html", me=auth.public_user(user),
                           is_admin=False, menus=[], rail="")


# --- PWA : 서비스워커는 루트 스코프에서 제공해야 사이트 전체를 제어할 수 있다 ---
@app.get("/sw.js")
def service_worker():
    resp = send_file(os.path.join(app.static_folder, "sw.js"), mimetype="text/javascript")
    resp.headers["Service-Worker-Allowed"] = "/"
    resp.headers["Cache-Control"] = "no-cache"
    return resp


@app.get("/manifest.webmanifest")
def manifest():
    return send_file(os.path.join(app.static_folder, "manifest.webmanifest"),
                     mimetype="application/manifest+json")


# ---------------------------------------------------------------------------
# 내 정보
# ---------------------------------------------------------------------------
@app.get("/api/me")
@auth.login_required
def me():
    user = auth.current_user()
    return jsonify(
        ok=True,
        user=auth.public_user(user),
        is_admin=permissions.is_admin(user),
        csrf_token=auth.csrf_token(),
        limits={
            "max_images": config.MAX_IMAGES_PER_MESSAGE,
            "max_upload_mb": config.MAX_UPLOAD_MB,
            "max_input_chars": config.MAX_INPUT_CHARS,
            "allowed_ext": sorted(config.ALLOWED_IMAGES),
            "min_password_length": config.MIN_PASSWORD_LENGTH,
        },
    )


@app.post("/api/me/password")
@auth.login_required
def change_my_password():
    from werkzeug.security import check_password_hash

    db = get_db()
    user = auth.current_user()
    data = request.get_json(silent=True) or {}
    current = data.get("current_password") or ""
    new = data.get("new_password") or ""

    if not check_password_hash(user["password_hash"], current):
        abort(400, "현재 비밀번호가 올바르지 않습니다.")
    try:
        auth.set_password(db, user["id"], new)
    except ValueError as exc:
        abort(400, str(exc))
    audit(db, user["id"], "password_changed", "user", user["id"])
    db.commit()
    # 비밀번호 지문이 바뀌었으므로 현재 세션을 다시 발급한다. (다른 기기는 로그아웃)
    auth.login_session(db, auth.user_by_id(db, user["id"]))
    db.commit()
    return jsonify(ok=True)


# ---------------------------------------------------------------------------
# 프로젝트 API  (조회: 로그인 사용자 / 변경: 관리자)
# ---------------------------------------------------------------------------
@app.get("/api/projects")
@auth.login_required
@auth.menu_required("chat")
def list_projects():
    db = get_db()
    user = auth.current_user()
    # 프로젝트의 세션 개수는 "내가 볼 수 있는 세션" 기준으로 센다.
    # (남의 private 세션 개수가 숫자로 새어나가지 않게 한다)
    where, params = permissions.visible_sessions_clause(user, "all")
    sql = ("SELECT p.*, (SELECT COUNT(*) FROM sessions s "
           "             WHERE s.project_id = p.id AND " + where + ") AS session_count "
           "FROM projects p ORDER BY p.id")
    rows = db.execute(sql, params).fetchall()
    return jsonify(ok=True, projects=[row_to_dict(r) for r in rows],
                   can_manage=permissions.can_manage_project(user))


@app.post("/api/projects")
@auth.login_required
@auth.menu_required("chat")
def create_project():
    user = auth.current_user()
    permissions.require_manage_project(user)
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    description = (data.get("description") or "").strip()
    if not name:
        abort(400, "프로젝트 이름을 입력해 주세요.")
    if len(name) > 200:
        abort(400, "프로젝트 이름이 너무 깁니다. (최대 200자)")

    db = get_db()
    now = ts()
    cur = db.execute(
        "INSERT INTO projects (name, description, created_at, updated_at) VALUES (?,?,?,?)",
        (name, description[:2000], now, now))
    audit(db, user["id"], "project_created", "project", cur.lastrowid, "name=%s" % name)
    db.commit()
    row = db.execute("SELECT * FROM projects WHERE id = ?", (cur.lastrowid,)).fetchone()
    return jsonify(ok=True, project=row_to_dict(row)), 201


@app.patch("/api/projects/<int:pid>")
@auth.login_required
@auth.menu_required("chat")
def update_project(pid):
    user = auth.current_user()
    permissions.require_manage_project(user)
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
    values += [ts(), pid]
    db.execute("UPDATE projects SET %s WHERE id = ?" % ", ".join(fields), values)
    audit(db, user["id"], "project_updated", "project", pid)
    db.commit()
    row = db.execute("SELECT * FROM projects WHERE id = ?", (pid,)).fetchone()
    return jsonify(ok=True, project=row_to_dict(row))


@app.delete("/api/projects/<int:pid>")
@auth.login_required
@auth.menu_required("chat")
def delete_project(pid):
    user = auth.current_user()
    permissions.require_manage_project(user)
    db = get_db()
    row = get_project_or_404(db, pid)

    # DB 먼저 정리(ON DELETE CASCADE) -> 커밋 후 파일 정리.
    # 파일 삭제가 실패해도 DB 트랜잭션에는 영향이 없다.
    db.execute("DELETE FROM projects WHERE id = ?", (pid,))
    audit(db, user["id"], "project_deleted", "project", pid, "name=%s" % row["name"])
    db.commit()

    files_ok = remove_tree(os.path.join(UPLOAD_DIR, "project_%d" % pid))
    return jsonify(ok=True, deleted=pid, files_removed=files_ok)


# ---------------------------------------------------------------------------
# 세션 API
# ---------------------------------------------------------------------------
@app.get("/api/projects/<int:pid>/sessions")
@auth.login_required
@auth.menu_required("chat")
def list_sessions(pid):
    """
    scope : all(기본) | mine | public
    q     : 이름 LIKE 검색
    권한 필터는 SQL 단계에서 걸린다. 프론트에서 숨기는 방식이 아니다.
    """
    db = get_db()
    user = auth.current_user()
    get_project_or_404(db, pid)

    scope = (request.args.get("scope") or "all").lower()
    if scope not in ("all", "mine", "public"):
        scope = "all"
    where, params = permissions.visible_sessions_clause(user, scope)

    sql = ("SELECT s.*, u.username AS owner_username, u.display_name AS owner_display_name,"
           " (SELECT COUNT(*) FROM messages m WHERE m.session_id = s.id) AS message_count "
           "FROM sessions s LEFT JOIN users u ON u.id = s.owner_id "
           "WHERE s.project_id = ? AND " + where)
    args = [pid] + params

    q = (request.args.get("q") or "").strip()
    if q:
        sql += " AND s.name LIKE ? ESCAPE '\\'"
        args.append("%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%")

    sql += " ORDER BY s.updated_at DESC, s.id DESC"
    rows = db.execute(sql, args).fetchall()
    return jsonify(ok=True, sessions=[session_payload(db, user, r) for r in rows],
                   scope=scope)


def resolve_server_id(db, user, raw):
    """
    대화에 붙일 서버를 확인한다. 반환: id 또는 None (서버 없이 쓰기)

    화면에서 고른 것을 믿지 않는다. 질문마다 다시 보는 자리는 post_message 이고,
    여기는 "붙일 때" 의 검사다. 둘 다 있어야 한다. 허용이 끊긴 뒤에도 그 대화가
    계속 돌아가면 끊은 것이 아니다.
    """
    if raw in (None, "", 0, "0"):
        return None
    try:
        server_id = int(raw)
    except (TypeError, ValueError):
        abort(400, "서버를 고르세요.")
    permissions.require_menu(db, user, "servers")
    row = relay_store.get_server(db, server_id)
    permissions.require_server(db, user, row)
    if not row["is_enabled"]:
        abort(409, "꺼 둔 서버입니다. 관리자에게 문의하세요.")
    return server_id


def check_server_visibility(server_id, visibility):
    """
    서버에 붙은 대화는 공개로 둘 수 없다.

    안에 서버 출력이 들어 있다. 공개 대화는 로그인한 사람 누구나 읽고 쓸 수
    있으므로, 서버를 붙인 채 공개로 두면 허용받지 않은 사람이 그 서버의
    출력을 읽고 그 대화에서 질문까지 할 수 있다.
    """
    if server_id and visibility == permissions.PUBLIC:
        abort(400, "서버를 붙인 대화는 전체 공개로 둘 수 없습니다. "
                   "안에 서버 출력이 들어 있습니다.")


def _create_session_row(db, user, pid, data):
    name = (data.get("name") or "").strip() or "새 대화"
    visibility = (data.get("visibility") or permissions.PRIVATE).strip().lower()
    if visibility not in permissions.VISIBILITIES:
        abort(400, "공개 범위가 올바르지 않습니다.")
    server_id = resolve_server_id(db, user, data.get("server_id"))
    check_server_visibility(server_id, visibility)

    now = ts()
    cur = db.execute(
        "INSERT INTO sessions (project_id, owner_id, name, visibility, claude_session_id,"
        " server_id, created_at, updated_at) VALUES (?,?,?,?,NULL,?,?,?)",
        (pid, user["id"], name[:200], visibility, server_id, now, now))
    db.execute("UPDATE projects SET updated_at = ? WHERE id = ?", (now, pid))
    if server_id:
        audit(db, user["id"], "session_server_bound", "session", cur.lastrowid,
              "server=%d" % server_id)
    db.commit()
    return get_session_row(db, cur.lastrowid)


@app.post("/api/projects/<int:pid>/sessions")
@auth.login_required
@auth.menu_required("chat")
def create_session(pid):
    db = get_db()
    user = auth.current_user()
    get_project_or_404(db, pid)
    row = _create_session_row(db, user, pid, request.get_json(silent=True) or {})
    return jsonify(ok=True, session=session_payload(db, user, row)), 201


@app.post("/api/sessions")
@auth.login_required
@auth.menu_required("chat")
def create_session_anywhere():
    """
    프로젝트를 고르지 않고 대화를 만든다. 서버 목록에서 「채팅」을 눌렀을 때
    쓰는 길이다. 그 화면에는 프로젝트라는 개념이 없다.

    project_id 를 주지 않으면 최근에 쓴 프로젝트에 넣고, 그것도 없으면 가장
    오래된 프로젝트에 넣는다. 프로젝트가 하나도 없으면 만들지 않고 409 다.
    프로젝트를 만드는 것은 관리자의 일이고, 여기서 몰래 만들면 목록에 정체를
    알 수 없는 프로젝트가 생긴다.
    """
    db = get_db()
    user = auth.current_user()
    data = request.get_json(silent=True) or {}

    pid = data.get("project_id")
    if pid:
        get_project_or_404(db, int(pid))
    else:
        row = db.execute(
            "SELECT project_id FROM sessions WHERE owner_id = ?"
            " ORDER BY updated_at DESC LIMIT 1", (user["id"],)).fetchone()
        if row:
            pid = row["project_id"]
        else:
            first = db.execute("SELECT id FROM projects ORDER BY id LIMIT 1").fetchone()
            if first is None:
                abort(409, "먼저 관리자가 프로젝트를 하나 만들어야 합니다.")
            pid = first["id"]

    row = _create_session_row(db, user, int(pid), data)
    return jsonify(ok=True, session=session_payload(db, user, row)), 201


@app.get("/api/sessions/<int:sid>")
@auth.login_required
@auth.menu_required("chat")
def get_session(sid):
    db = get_db()
    user = auth.current_user()
    row = get_session_or_404(db, sid)
    permissions.require_view_session(db, user, row)
    return jsonify(ok=True, session=session_payload(db, user, row))


@app.patch("/api/sessions/<int:sid>")
@auth.login_required
@auth.menu_required("chat")
def update_session(sid):
    db = get_db()
    user = auth.current_user()
    row = get_session_or_404(db, sid)
    permissions.require_manage_session(db, user, row)

    data = request.get_json(silent=True) or {}
    fields, values, changed_visibility = [], [], None
    changed_server = False

    if "name" in data:
        name = (data.get("name") or "").strip()
        if not name:
            abort(400, "세션 이름을 입력해 주세요.")
        fields.append("name = ?")
        values.append(name[:200])
    if "visibility" in data:
        vis = (data.get("visibility") or "").strip().lower()
        if vis not in permissions.VISIBILITIES:
            abort(400, "공개 범위가 올바르지 않습니다.")
        fields.append("visibility = ?")
        values.append(vis)
        changed_visibility = vis
    new_server = None
    if "server_id" in data:
        # 대화 안에서 서버를 바꾼다. 바뀌는 것은 **그 다음 질문부터**다.
        # 이미 받은 답은 어느 서버에서 나온 것인지 그대로 남는다. (고치지 않는다)
        new_server = resolve_server_id(db, user, data.get("server_id"))
        fields.append("server_id = ?")
        values.append(new_server)
        changed_server = True
    if data.get("reset_claude_session"):
        fields.append("claude_session_id = NULL")
    if not fields:
        abort(400, "변경할 내용이 없습니다.")

    # 둘을 한 번에 바꿀 때도, 하나만 바꿀 때도 같은 규칙을 본다.
    final_server = new_server if changed_server else row["server_id"]
    final_vis = changed_visibility or row["visibility"]
    check_server_visibility(final_server, final_vis)

    fields.append("updated_at = ?")
    values += [ts(), sid]
    db.execute("UPDATE sessions SET %s WHERE id = ?" % ", ".join(fields), values)
    if changed_visibility:
        audit(db, user["id"], "session_visibility_changed", "session", sid,
              "to=%s" % changed_visibility)
    if changed_server:
        audit(db, user["id"], "session_server_changed", "session", sid,
              "to=%s" % (final_server or "none"))
    db.commit()
    return jsonify(ok=True, session=session_payload(db, user, get_session_row(db, sid)))


@app.delete("/api/sessions/<int:sid>")
@auth.login_required
@auth.menu_required("chat")
def delete_session(sid):
    db = get_db()
    user = auth.current_user()
    row = get_session_or_404(db, sid)
    permissions.require_manage_session(db, user, row)
    pid = row["project_id"]

    db.execute("DELETE FROM sessions WHERE id = ?", (sid,))
    db.execute("UPDATE projects SET updated_at = ? WHERE id = ?", (ts(), pid))
    audit(db, user["id"], "session_deleted", "session", sid, "name=%s" % row["name"])
    db.commit()

    files_ok = remove_tree(os.path.join(UPLOAD_DIR, "project_%d" % pid, "session_%d" % sid))
    with _SESSION_LOCKS_GUARD:
        _SESSION_LOCKS.pop(sid, None)
    return jsonify(ok=True, deleted=sid, files_removed=files_ok)


# ---------------------------------------------------------------------------
# 메시지 API
# ---------------------------------------------------------------------------
@app.get("/api/sessions/<int:sid>/messages")
@auth.login_required
@auth.menu_required("chat")
def list_messages(sid):
    db = get_db()
    user = auth.current_user()
    row = get_session_or_404(db, sid)
    permissions.require_view_session(db, user, row)
    return jsonify(ok=True, messages=message_payload(db, sid),
                   session=session_payload(db, user, row))


@app.post("/api/sessions/<int:sid>/messages")
@auth.login_required
@auth.menu_required("chat")
def post_message(sid):
    """
    multipart/form-data : message=텍스트, images=파일(복수)
    application/json    : {"message": "..."}
    """
    db = get_db()
    user = auth.current_user()
    sess = get_session_or_404(db, sid)
    permissions.require_write_session(db, user, sess)

    if request.files or request.form:
        text = request.form.get("message", "")
        files = [f for f in request.files.getlist("images") if f and f.filename]
    else:
        data = request.get_json(silent=True) or {}
        text = data.get("message", "")
        files = []

    # --- 서버가 붙은 대화라면 질문마다 다시 본다 ---------------------------
    # 화면에서 고른 것을 믿지 않는다. 허용이 끊긴 뒤에도 그 대화가 계속
    # 돌아가면 끊은 것이 아니다. 사용자 메시지를 저장하기 전에 본다.
    server_row = None
    if sess["server_id"]:
        server_row = relay_store.get_server(db, sess["server_id"])
        if server_row is None:
            abort(409, "이 대화에 붙어 있던 서버가 목록에서 사라졌습니다. "
                       "아래 서버 칩에서 다시 고르거나 '서버 없이 쓰기' 로 "
                       "바꿔 주세요.")
        if not server_row["is_enabled"]:
            abort(409, "%s 서버는 지금 꺼져 있습니다. 다른 서버를 고르거나 "
                       "'서버 없이 쓰기' 로 바꿔 주세요." % server_row["name"])
        if not permissions.can_use_server(db, user, server_row):
            abort(403, "%s 서버를 쓸 허용이 없습니다. 관리자에게 요청하거나 "
                       "'서버 없이 쓰기' 로 바꿔 주세요." % server_row["name"])

    if not isinstance(text, str):
        abort(400, "잘못된 요청 형식입니다.")
    text = text.replace("\r\n", "\n").strip()
    if not text and not files:
        abort(400, "질문을 입력해 주세요.")
    if len(text) > config.MAX_INPUT_CHARS:
        abort(413, "입력이 너무 깁니다. (%d자 / 최대 %d자)"
              % (len(text), config.MAX_INPUT_CHARS))
    if len(files) > config.MAX_IMAGES_PER_MESSAGE:
        abort(400, "이미지는 한 번에 최대 %d개까지 첨부할 수 있습니다. (요청 %d개)"
              % (config.MAX_IMAGES_PER_MESSAGE, len(files)))

    # --- 업로드 검증/저장 (DB 기록 전에 끝낸다) -----------------------------
    saved = []
    for f in files:
        info, err = save_upload(f, sess["project_id"], sid)
        if err:
            discard_uploads(saved)
            abort(400, err)
        saved.append(info)

    if not text:
        text = "첨부한 이미지를 확인해줘."

    provider = get_provider(db)
    limit = settings_store.get_int(db, "max_concurrent_claude", 3)

    # --- 세션 단위 lock : 같은 세션 동시 요청 차단 --------------------------
    lock = session_lock(sid)
    if not lock.acquire(blocking=False):
        discard_uploads(saved)
        abort(409, "이 세션은 현재 다른 사용자의 요청을 처리 중입니다. "
                   "잠시 후 다시 시도해 주세요.")
    try:
        if not _LIMITER.acquire(max(1, limit)):
            discard_uploads(saved)
            abort(429, "서버가 처리 중인 요청이 많습니다(최대 %d개). "
                       "잠시 후 다시 시도해 주세요." % limit)
        try:
            now = ts()
            cur = db.execute(
                "INSERT INTO messages (session_id, user_id, role, content, created_at) "
                "VALUES (?,?,?,?,?)", (sid, user["id"], "user", text, now))
            user_msg_id = cur.lastrowid
            for s in saved:
                db.execute(
                    "INSERT INTO attachments (session_id, message_id, original_name,"
                    " stored_name, file_path, mime_type, file_size, created_at)"
                    " VALUES (?,?,?,?,?,?,?,?)",
                    (sid, user_msg_id, s["original_name"], s["stored_name"],
                     s["file_path"], s["mime_type"], s["file_size"], now))
            touch_session(db, sid)
            db.commit()

            started = time.time()
            images = [(s["original_name"], s["abs_path"], s["mime_type"]) for s in saved]
            author = user["display_name"] or user["username"]

            # 서버가 붙은 대화에만 안내를 앞에 붙인다. 안 붙은 대화는 지금까지와
            # 글자 하나도 다르지 않게 동작한다.
            asked = text
            if server_row is not None:
                asked = relay_module.chat_prompt_prefix(
                    db, server_row, permissions.ssh_level(db, user),
                    relay_store.chat_max_commands(db)) + text

            ok, reply, mode = ask_claude(db, provider, sess, asked, images,
                                         user_msg_id, author=author)

            cmd_ids, cmd_note = [], None
            if ok and server_row is not None:
                def ask_again(prompt):
                    r_ok, r_text, _mode = ask_claude(db, provider, sess, prompt, [],
                                                     None, author=author)
                    return r_ok, r_text

                reply, cmd_ids, cmd_note = relay_module.handle_chat_reply(
                    db, user, sess, reply, ask_again,
                    deadline=started + config.RELAY_CHAT_BUDGET,
                    claude_timeout=settings_store.get_int(db, "claude_timeout", 180))
            elapsed = round(time.time() - started, 2)

            cur = db.execute(
                "INSERT INTO messages (session_id, user_id, role, content, created_at) "
                "VALUES (?,NULL,?,?,?)",
                (sid, "assistant" if ok else "error", reply, ts()))
            relay_module.attach_commands(db, cur.lastrowid, cmd_ids)
            touch_session(db, sid)
            db.commit()
        finally:
            _LIMITER.release()
    finally:
        lock.release()

    if not ok:
        app.logger.warning("claude 실패 (session %s, %s): %s", sid, mode,
                           (reply or "").splitlines()[0])

    messages = message_payload(db, sid)
    return jsonify(ok=ok, mode=mode, elapsed=elapsed,
                   messages=messages[-2:], error=None if ok else reply,
                   ssh=cmd_note)


# ---------------------------------------------------------------------------
# 첨부파일 API
#   - DB 의 id 로만 조회한다 (파일 경로를 URL 로 받지 않는다)
#   - 해당 첨부가 속한 세션의 조회 권한을 반드시 검사한다
# ---------------------------------------------------------------------------
@app.get("/api/attachments/<int:aid>")
@auth.login_required
@auth.menu_required("chat")
def get_attachment(aid):
    db = get_db()
    user = auth.current_user()
    row = db.execute("SELECT * FROM attachments WHERE id = ?", (aid,)).fetchone()
    if row is None:
        abort(404, "첨부파일을 찾을 수 없습니다.")

    sess = get_session_row(db, row["session_id"])
    if sess is None:
        abort(404, "첨부파일을 찾을 수 없습니다.")
    # private 세션의 이미지는 소유자만. URL 을 알아도 접근할 수 없다.
    permissions.require_view_session(db, user, sess)

    path = os.path.realpath(abs_upload_path(row["file_path"]))
    root = os.path.realpath(UPLOAD_DIR)
    try:
        inside = os.path.commonpath([path, root]) == root
    except ValueError:  # 드라이브가 다르면 commonpath 가 예외를 낸다
        inside = False
    if not inside:
        abort(403, "허용되지 않는 경로입니다.")
    if not os.path.isfile(path):
        abort(404, "파일이 존재하지 않습니다.")

    resp = send_file(path, mimetype=row["mime_type"],
                     download_name=row["original_name"], as_attachment=False)
    resp.headers["Cache-Control"] = "private, no-store"
    return resp


# ---------------------------------------------------------------------------
# 기동 / 관리 명령
# ---------------------------------------------------------------------------
def _patch_scan_loop():
    """
    주기 스캔. 루트마다 주기가 따로 있고(patch_roots.scan_interval_s),
    오래 안 돈 루트부터 돈다. 한 루트가 죽어도 나머지는 돈다.

    요청 스레드가 아니라 전용 스레드라서 Flask 의 g 를 쓸 수 없다. 연결을
    직접 열고 닫는다. gunicorn 워커가 1개이므로 이 스레드도 하나뿐이고,
    patch_scan 의 락이 수동 검사와 겹치는 것을 막는다.
    """
    while True:
        time.sleep(max(10, config.PATCH_SCAN_TICK_SECONDS))
        try:
            conn = connect()
            try:
                for root in patch_scan.due_roots(conn):
                    patch_scan.scan(conn, root["id"], "periodic")
            finally:
                conn.close()
        except Exception:                      # pragma: no cover
            app.logger.exception("patch scan loop")


def start_patch_scanner():
    if not config.PATCH_SCAN_ENABLED:
        return
    t = threading.Thread(target=_patch_scan_loop, name="patch-scan", daemon=True)
    t.start()


def startup():
    """마이그레이션 -> 설정 초기값 -> bootstrap 토큰. 매 기동 시 안전하게 반복 가능."""
    info = migrate()
    conn = connect()
    try:
        settings_store.bootstrap(conn)
        token = auth.ensure_setup_token(conn)
    finally:
        conn.close()
    start_patch_scanner()
    relay_module.start_housekeeping(app)
    return info, token


def _ask(prompt, secret=False):
    """
    대화형이면 보통대로 묻고, 파이프로 넘어오면 한 줄씩 읽는다.

    자동화(ansible 등)에서 다음처럼 쓸 수 있게 하기 위한 것이다.
        printf 'admin\n관리자\npw\npw\n' | python app.py create-admin
    (Windows 의 getpass 는 파이프 대신 콘솔을 직접 읽어 멈춘다)
    """
    if not sys.stdin.isatty():
        line = sys.stdin.readline()
        if not line:
            raise EOFError("입력이 부족합니다.")
        return line.rstrip("\r\n")
    if secret:
        return getpass.getpass(prompt)
    return input(prompt)


def cmd_create_admin():
    conn = connect()
    try:
        try:
            username = _ask("관리자 아이디: ").strip()
            display = _ask("표시 이름(비우면 아이디와 동일): ").strip()
            pw1 = _ask("비밀번호: ", secret=True)
            pw2 = _ask("비밀번호 확인: ", secret=True)
        except EOFError as exc:
            print("오류: %s" % exc)
            return 1
        if pw1 != pw2:
            print("비밀번호가 서로 다릅니다.")
            return 1
        try:
            uid = auth.create_user(conn, username, pw1, display, role="admin")
        except ValueError as exc:
            print("오류: %s" % exc)
            return 1
        audit(conn, uid, "admin_bootstrapped", "user", uid, "via=cli")
        conn.commit()
        auth.ensure_setup_token(conn)
        print("관리자 계정을 만들었습니다: %s (id=%d)" % (username, uid))
        return 0
    finally:
        conn.close()


def main(argv):
    cmd = argv[1] if len(argv) > 1 else "run"

    info, token = STARTUP_INFO, STARTUP_TOKEN

    if cmd == "migrate":
        print("마이그레이션 완료." if info["applied"] else "변경할 내용이 없습니다.")
        if info["backup"]:
            print("백업: %s" % info["backup"])
        return 0
    if cmd == "backup":
        print("백업 생성: %s" % backup_database("manual"))
        return 0
    if cmd == "create-admin":
        return cmd_create_admin()
    if cmd not in ("run", "serve"):
        print(__doc__)
        return 2

    if info["applied"]:
        app.logger.warning("DB 마이그레이션 적용: %s", " / ".join(info["steps"]))
    if token:
        app.logger.warning(
            "=" * 70 + "\n"
            "아직 관리자 계정이 없습니다. 브라우저에서 /setup 을 열고 아래 토큰을 입력하세요.\n"
            "  bootstrap token : %s\n"
            "  (파일)          : %s\n"
            "셸에서 바로 만들려면: python app.py create-admin\n" % (
                token, auth.SETUP_TOKEN_FILE) + "=" * 70)

    conn = connect()
    try:
        cfg = settings_store.snapshot(conn)
    finally:
        conn.close()
    app.logger.warning(
        "claude-web starting: provider=%s cli=%s workdir=%s db=%s uploads=%s "
        "timeout=%ss concurrency=%s port=%s",
        cfg["provider"], cfg["cli_path"], cfg["workdir"] or os.getcwd(),
        config.DATABASE_PATH, UPLOAD_DIR, cfg["timeout"], cfg["max_concurrent"], config.PORT)
    app.run(host=config.HOST, port=config.PORT, threaded=True, debug=False)
    return 0


# WSGI(gunicorn)로 띄울 때도 마이그레이션이 돌도록 import 시점에 한 번 실행한다.
STARTUP_INFO, STARTUP_TOKEN = startup()

if __name__ == "__main__":
    sys.exit(main(sys.argv))
