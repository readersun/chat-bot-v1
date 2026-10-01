#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
auth
====

로컬 계정 로그인 / CSRF / 로그인 rate limit / 최초 관리자 bootstrap.

- 비밀번호는 werkzeug 의 scrypt 해시로만 저장한다. 평문은 어디에도 남기지 않는다.
- 인증 수단은 서버 서명 쿠키(Flask session)뿐이다. localStorage 는 UI 상태 전용.
- CSRF 는 세션에 저장한 토큰을 X-CSRF-Token 헤더 또는 _csrf 폼 필드로 다시 받는
  double submit 방식이다. (fetch 기반 SPA 구조에 맞고 추가 의존성이 없다)

최초 관리자
-----------
users 가 비어 있을 때만 /setup 이 열린다. 다만 "먼저 접속한 사람이 관리자가
되는" 경쟁을 막기 위해 서버 기동 시 만든 1회용 bootstrap 토큰을 함께 요구한다.
토큰은 서버 로그(journalctl)와 data/setup-token.txt(0600)에 남는다.
서버 셸에 접근할 수 있는 사람만 관리자를 만들 수 있다는 뜻이다.

셸에서 바로 만들고 싶으면:  python app.py create-admin
"""

import functools
import hmac
import os
import re
import secrets
import stat

from flask import (
    Blueprint, abort, current_app, g, jsonify, redirect, render_template,
    request, session, url_for,
)
from werkzeug.security import check_password_hash, generate_password_hash

import config
from db import audit, get_db, ts

bp = Blueprint("auth", __name__)

USERNAME_RE = re.compile(r"^[A-Za-z0-9._-]{3,32}$")
SETUP_TOKEN_FILE = os.path.join(os.path.dirname(config.DATABASE_PATH), "setup-token.txt")

_setup_token = None


# ---------------------------------------------------------------------------
# 사용자 helper
# ---------------------------------------------------------------------------
def user_by_name(db, username):
    return db.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()


def user_by_id(db, uid):
    return db.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()


def admin_exists(db):
    return db.execute(
        "SELECT 1 FROM users WHERE role = 'admin' AND is_active = 1 LIMIT 1"
    ).fetchone() is not None


def any_user_exists(db):
    return db.execute("SELECT 1 FROM users LIMIT 1").fetchone() is not None


def validate_username(name):
    name = (name or "").strip()
    if not USERNAME_RE.match(name):
        raise ValueError("아이디는 영문/숫자/._- 조합 3~32자여야 합니다.")
    return name


def validate_password(pw):
    pw = pw or ""
    if len(pw) < config.MIN_PASSWORD_LENGTH:
        raise ValueError("비밀번호는 %d자 이상이어야 합니다." % config.MIN_PASSWORD_LENGTH)
    if len(pw) > 200:
        raise ValueError("비밀번호가 너무 깁니다.")
    return pw


def create_user(db, username, password, display_name="", role="user", is_active=1):
    username = validate_username(username)
    validate_password(password)
    if role not in ("admin", "user"):
        raise ValueError("역할이 올바르지 않습니다.")
    if user_by_name(db, username) is not None:
        raise ValueError("이미 존재하는 아이디입니다: %s" % username)
    now = ts()
    cur = db.execute(
        "INSERT INTO users (username, password_hash, display_name, role, is_active,"
        " created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
        (username, generate_password_hash(password),
         (display_name or username).strip()[:100], role, 1 if is_active else 0, now, now))
    uid = cur.lastrowid

    # 기본 메뉴를 함께 넣는다. 안 넣으면 새 사용자가 로그인하자마자 "권한 없음"
    # 화면을 본다. 관리자는 permissions.user_menus 가 알아서 전부 돌려주므로
    # 행을 따로 만들지 않는다.
    if role != "admin":
        import permissions
        db.executemany(
            "INSERT OR IGNORE INTO user_menus (user_id, menu_key, granted_at)"
            " VALUES (?,?,?)",
            [(uid, k, now) for k in permissions.DEFAULT_NEW_USER_MENUS])
    return uid


def set_password(db, uid, password):
    validate_password(password)
    db.execute("UPDATE users SET password_hash = ?, updated_at = ? WHERE id = ?",
               (generate_password_hash(password), ts(), uid))


def public_user(row):
    if row is None:
        return None
    return {
        "id": row["id"],
        "username": row["username"],
        "display_name": row["display_name"] or row["username"],
        "role": row["role"],
        "is_active": bool(row["is_active"]),
        "last_login_at": row["last_login_at"],
        "created_at": row["created_at"],
    }


# ---------------------------------------------------------------------------
# 현재 사용자
# ---------------------------------------------------------------------------
def load_current_user():
    """before_request 에서 호출. 비활성 계정은 즉시 로그아웃된다."""
    g.user = None
    uid = session.get("uid")
    if not uid:
        return
    row = user_by_id(get_db(), uid)
    if row is None or not row["is_active"]:
        session.clear()
        return
    # 비밀번호가 바뀌면 기존 쿠키를 무효화한다.
    if session.get("pw") != _pw_stamp(row):
        session.clear()
        return
    g.user = row


def current_user():
    return getattr(g, "user", None)


def _pw_stamp(row):
    """세션 쿠키에 넣을 비밀번호 지문. 해시 자체를 쿠키에 담지 않는다."""
    return hmac.new(config.SECRET_KEY.encode("utf-8"),
                    (row["password_hash"] or "").encode("utf-8"), "sha256").hexdigest()[:16]


def login_session(db, row):
    session.clear()
    session["uid"] = row["id"]
    session["pw"] = _pw_stamp(row)
    session.permanent = True
    db.execute("UPDATE users SET last_login_at = ? WHERE id = ?", (ts(), row["id"]))


def wants_json():
    if request.path.startswith("/api/"):
        return True
    accept = request.headers.get("Accept", "")
    return "application/json" in accept and "text/html" not in accept


def login_required(fn):
    @functools.wraps(fn)
    def wrapper(*a, **kw):
        if current_user() is None:
            if wants_json():
                abort(401, "로그인이 필요합니다.")
            return redirect(url_for("auth.login", next=request.full_path.rstrip("?")))
        return fn(*a, **kw)
    return wrapper


def admin_required(fn):
    @functools.wraps(fn)
    def wrapper(*a, **kw):
        user = current_user()
        if user is None:
            if wants_json():
                abort(401, "로그인이 필요합니다.")
            return redirect(url_for("auth.login", next=request.full_path.rstrip("?")))
        if user["role"] != "admin":
            abort(403, "관리자 권한이 필요합니다.")
        return fn(*a, **kw)
    return wrapper


def menu_required(key):
    """
    메뉴 권한 검사. 화면 라우트와 API 양쪽에 건다.

    레일에서 항목을 안 그리는 것은 장식이다. 이 데코레이터를 API 하나에
    빼먹으면 권한 없는 사람이 curl 한 줄로 목록을 받아 간다.
    """
    def deco(fn):
        @functools.wraps(fn)
        def wrapper(*a, **kw):
            import permissions
            user = current_user()
            if user is None:
                if wants_json():
                    abort(401, "로그인이 필요합니다.")
                return redirect(url_for("auth.login", next=request.full_path.rstrip("?")))
            permissions.require_menu(get_db(), user, key)
            return fn(*a, **kw)
        return wrapper
    return deco


# ---------------------------------------------------------------------------
# CSRF
# ---------------------------------------------------------------------------
SAFE_METHODS = ("GET", "HEAD", "OPTIONS")


def csrf_token():
    tok = session.get("_csrf")
    if not tok:
        tok = secrets.token_urlsafe(32)
        session["_csrf"] = tok
    return tok


def check_csrf():
    """before_request 에서 호출. 상태를 바꾸는 요청에만 적용한다."""
    if request.method in SAFE_METHODS:
        return
    sent = request.headers.get("X-CSRF-Token") or request.form.get("_csrf") or ""
    expected = session.get("_csrf") or ""
    if not expected or not sent or not hmac.compare_digest(str(sent), str(expected)):
        abort(400, "요청이 만료되었습니다. 페이지를 새로고침한 뒤 다시 시도해 주세요.")


# ---------------------------------------------------------------------------
# 로그인 rate limit (SQLite. Redis 같은 외부 저장소를 쓰지 않는다)
# ---------------------------------------------------------------------------
def client_ip():
    if config.TRUST_PROXY:
        fwd = request.headers.get("X-Forwarded-For", "")
        if fwd:
            return fwd.split(",")[0].strip()[:64]
    return (request.remote_addr or "")[:64]


def _window_expr():
    return "datetime('now', 'localtime', '-%d minutes')" % config.LOGIN_WINDOW_MINUTES


def login_blocked(db, username, ip):
    """
    계정 기준과 IP 기준을 다른 한도로 본다.

    - 계정 : LOGIN_MAX_FAILURES (기본 5). brute force 의 주 방어선.
    - IP   : 그 값의 LOGIN_IP_FACTOR 배 (기본 20). 사내 NAT/프록시 뒤에서는
             여러 사람이 같은 IP 로 보이므로 계정과 같은 한도를 적용하면
             한 사람의 오타 몇 번으로 전원이 로그인하지 못하게 된다.
             (그래도 한 IP 에서 대량으로 시도하는 경우는 막힌다)
    """
    sql = ("SELECT COUNT(*) AS c FROM login_attempts "
           "WHERE success = 0 AND created_at >= %s AND %s")
    by_user = db.execute(sql % (_window_expr(), "username = ?"), (username,)).fetchone()["c"]
    by_ip = db.execute(sql % (_window_expr(), "ip = ?"), (ip,)).fetchone()["c"]
    return (by_user >= config.LOGIN_MAX_FAILURES
            or by_ip >= config.LOGIN_MAX_FAILURES * max(1, config.LOGIN_IP_FACTOR))


def record_attempt(db, username, ip, success):
    db.execute(
        "INSERT INTO login_attempts (username, ip, success, created_at) VALUES (?,?,?,?)",
        (username[:64], ip, 1 if success else 0, ts()))
    if success:
        db.execute("DELETE FROM login_attempts WHERE username = ? AND success = 0",
                   (username[:64],))
    # 오래된 기록 정리 (테이블이 무한정 커지지 않게)
    db.execute("DELETE FROM login_attempts WHERE created_at < datetime('now','localtime','-1 day')")


# ---------------------------------------------------------------------------
# 최초 관리자 bootstrap
# ---------------------------------------------------------------------------
def ensure_setup_token(db):
    """관리자가 없으면 1회용 토큰을 만들고, 있으면 흔적을 지운다."""
    global _setup_token
    if admin_exists(db):
        _setup_token = None
        try:
            if os.path.exists(SETUP_TOKEN_FILE):
                os.remove(SETUP_TOKEN_FILE)
        except OSError:
            pass
        return None
    if _setup_token is None:
        _setup_token = secrets.token_urlsafe(24)
        try:
            with open(SETUP_TOKEN_FILE, "w", encoding="utf-8") as fh:
                fh.write(_setup_token + "\n")
            if os.name == "posix":
                os.chmod(SETUP_TOKEN_FILE, stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass
    return _setup_token


def setup_token():
    return _setup_token


def safe_next(target):
    """오픈 리다이렉트 방지: 같은 사이트의 절대 경로만 허용한다."""
    if not target:
        return None
    if not target.startswith("/") or target.startswith("//") or "\\" in target:
        return None
    if target.startswith("/login") or target.startswith("/setup"):
        return None
    return target


# ---------------------------------------------------------------------------
# 라우트
# ---------------------------------------------------------------------------
@bp.get("/login")
def login():
    db = get_db()
    if not any_user_exists(db):
        return redirect(url_for("auth.setup"))
    if current_user() is not None:
        return redirect(safe_next(request.args.get("next")) or "/")
    return render_template("login.html", csrf=csrf_token(),
                           next=safe_next(request.args.get("next")) or "", error=None)


@bp.post("/login")
def login_post():
    db = get_db()
    username = (request.form.get("username") or "").strip()
    password = request.form.get("password") or ""
    nxt = safe_next(request.form.get("next")) or "/"
    ip = client_ip()

    def fail(msg, status=401):
        return render_template("login.html", csrf=csrf_token(), next=nxt,
                               error=msg, username=username), status

    if not username or not password:
        return fail("아이디와 비밀번호를 입력하세요.", 400)

    if login_blocked(db, username, ip):
        audit(db, None, "login_blocked", "user", username, "ip=%s" % ip)
        db.commit()
        return fail("로그인 시도가 너무 많습니다. %d분 후에 다시 시도해 주세요."
                    % config.LOGIN_WINDOW_MINUTES, 429)

    row = user_by_name(db, username)
    ok = row is not None and check_password_hash(row["password_hash"], password)

    if not ok:
        record_attempt(db, username, ip, False)
        audit(db, row["id"] if row else None, "login_failed", "user", username, "ip=%s" % ip)
        db.commit()
        return fail("아이디 또는 비밀번호가 올바르지 않습니다.")

    if not row["is_active"]:
        record_attempt(db, username, ip, False)
        audit(db, row["id"], "login_denied_inactive", "user", username, "ip=%s" % ip)
        db.commit()
        return fail("비활성화된 계정입니다. 관리자에게 문의하세요.", 403)

    record_attempt(db, username, ip, True)
    login_session(db, row)
    audit(db, row["id"], "login", "user", row["id"], "ip=%s" % ip)
    db.commit()
    return redirect(nxt)


@bp.route("/logout", methods=["GET", "POST"])
def logout():
    user = current_user()
    if user is not None:
        db = get_db()
        audit(db, user["id"], "logout", "user", user["id"])
        db.commit()
    session.clear()
    if request.method == "POST" and wants_json():
        return jsonify(ok=True)
    return redirect(url_for("auth.login"))


@bp.get("/setup")
def setup():
    db = get_db()
    if admin_exists(db):
        abort(404, "최초 설정은 이미 완료되었습니다.")
    ensure_setup_token(db)
    return render_template("setup.html", csrf=csrf_token(), error=None,
                           token_file=SETUP_TOKEN_FILE)


@bp.post("/setup")
def setup_post():
    db = get_db()
    if admin_exists(db):
        abort(404, "최초 설정은 이미 완료되었습니다.")
    expected = ensure_setup_token(db)

    token = (request.form.get("token") or "").strip()
    username = (request.form.get("username") or "").strip()
    password = request.form.get("password") or ""
    confirm = request.form.get("password2") or ""
    display = (request.form.get("display_name") or "").strip()

    def fail(msg):
        return render_template("setup.html", csrf=csrf_token(), error=msg,
                               username=username, display_name=display,
                               token_file=SETUP_TOKEN_FILE), 400

    if not expected or not token or not hmac.compare_digest(token, expected):
        return fail("bootstrap 토큰이 올바르지 않습니다. 서버 로그 또는 %s 를 확인하세요."
                    % SETUP_TOKEN_FILE)
    if password != confirm:
        return fail("비밀번호가 서로 다릅니다.")
    try:
        uid = create_user(db, username, password, display, role="admin")
    except ValueError as exc:
        return fail(str(exc))

    audit(db, uid, "admin_bootstrapped", "user", uid)
    db.commit()
    ensure_setup_token(db)  # 토큰 파일 제거
    row = user_by_id(db, uid)
    login_session(db, row)
    db.commit()
    current_app.logger.warning("최초 관리자 계정이 생성되었습니다: %s", row["username"])
    return redirect("/")
