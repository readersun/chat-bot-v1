#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
admin
=====

관리자 전용 페이지와 API.

중요한 정책
-----------
- 모든 라우트에 admin_required 가 걸려 있다. 일반 사용자는 메뉴도, API 도 막힌다.
- 관리자라도 **private 세션의 대화 내용은 읽을 수 없다.** 여기서 다루는 세션
  정보는 메타데이터(이름/소유자/공개범위/메시지 개수)뿐이고, 본문을 돌려주는
  엔드포인트는 없다. (요구사항 27)
- 자격증명은 화면으로 내보내지 않는다. API Key 는 마스킹된 미리보기만,
  Claude CLI 인증은 "정상 / 실패" 상태만 보여준다.
- Storage 탭은 용량 "확인" 전용이다. 첨부파일 일괄 삭제나 자동 정리 기능은
  의도적으로 만들지 않았다. (실수로 운영 데이터가 사라지는 것을 막는다)
"""

import os
import shutil

from flask import Blueprint, abort, jsonify, render_template, request

import config
import providers
import settings_store
import storage as storage_module
from auth import (
    admin_required, create_user, current_user, public_user, set_password,
    user_by_id, validate_username,
)
from db import audit, get_db, row_to_dict, ts

bp = Blueprint("admin", __name__, url_prefix="/admin")
api = Blueprint("admin_api", __name__, url_prefix="/api/admin")

TABS = ("dashboard", "users", "claude", "system", "storage")


# ---------------------------------------------------------------------------
# 화면
# ---------------------------------------------------------------------------
@bp.get("/")
@admin_required
def home():
    return render_template("admin.html", tab="dashboard")


@bp.get("/<tab>")
@admin_required
def tab_page(tab):
    if tab not in TABS:
        abort(404, "없는 페이지입니다.")
    return render_template("admin.html", tab=tab)


# ---------------------------------------------------------------------------
# 대시보드 / 시스템
# ---------------------------------------------------------------------------
def _disk_free(path):
    try:
        usage = shutil.disk_usage(path)
        return {"total_gb": round(usage.total / 1073741824.0, 1),
                "free_gb": round(usage.free / 1073741824.0, 1)}
    except OSError:
        return None


def _db_size():
    total = 0
    for suffix in ("", "-wal", "-shm"):
        try:
            total += os.path.getsize(config.DATABASE_PATH + suffix)
        except OSError:
            pass
    return total


def _counts(db):
    one = lambda sql: db.execute(sql).fetchone()[0]  # noqa: E731
    return {
        "users": one("SELECT COUNT(*) FROM users"),
        "users_active": one("SELECT COUNT(*) FROM users WHERE is_active = 1"),
        "admins": one("SELECT COUNT(*) FROM users WHERE role = 'admin'"),
        "projects": one("SELECT COUNT(*) FROM projects"),
        "sessions": one("SELECT COUNT(*) FROM sessions"),
        "sessions_public": one("SELECT COUNT(*) FROM sessions WHERE visibility = 'public'"),
        "sessions_private": one("SELECT COUNT(*) FROM sessions WHERE visibility = 'private'"),
        "sessions_legacy": one("SELECT COUNT(*) FROM sessions WHERE owner_id IS NULL"),
        "messages": one("SELECT COUNT(*) FROM messages"),
        "attachments": one("SELECT COUNT(*) FROM attachments"),
    }


def _warnings(db):
    out = []
    if config.SECRET_KEY_IS_EPHEMERAL:
        out.append("SECRET_KEY 가 .env 에 없습니다. 서버를 재시작하면 모든 사용자가 "
                   "로그아웃됩니다. .env 에 고정값을 넣으세요.")
    if not config.SESSION_COOKIE_SECURE and request.scheme == "https":
        out.append("HTTPS 로 접속했지만 SESSION_COOKIE_SECURE 가 꺼져 있습니다. "
                   ".env 에서 SESSION_COOKIE_SECURE=1 을 권장합니다.")
    if config.SESSION_COOKIE_SECURE and request.scheme != "https":
        out.append("SESSION_COOKIE_SECURE 가 켜져 있는데 평문 HTTP 로 접속했습니다. "
                   "이 상태로는 로그인 쿠키가 저장되지 않습니다.")
    if not settings_store.encryption_available():
        out.append("cryptography 패키지가 없어 API Key 가 DB 에 평문으로 저장됩니다. "
                   "(DB 파일 권한은 0600 으로 제한됨) pip install cryptography 권장.")
    if db.execute("SELECT COUNT(*) FROM users WHERE role='admin' AND is_active=1")\
            .fetchone()[0] == 0:
        out.append("활성 관리자 계정이 없습니다.")
    return out


@api.get("/summary")
@admin_required
def summary():
    db = get_db()
    cfg = settings_store.snapshot(db)
    cfg["upload_dir"] = config.UPLOAD_DIR
    provider = providers.build(cfg)
    return jsonify(
        ok=True,
        counts=_counts(db),
        provider=provider.describe(),
        warnings=_warnings(db),
        system={
            "database_path": config.DATABASE_PATH,
            "database_size": _db_size(),
            "upload_dir": config.UPLOAD_DIR,
            "backup_dir": config.BACKUP_DIR,
            "disk": _disk_free(os.path.dirname(config.DATABASE_PATH)),
            "scheme": request.scheme,
            "session_cookie_secure": config.SESSION_COOKIE_SECURE,
            "trust_proxy": config.TRUST_PROXY,
            "secret_key_ephemeral": config.SECRET_KEY_IS_EPHEMERAL,
            "settings_encrypted": settings_store.encryption_available(),
            "max_upload_mb": config.MAX_UPLOAD_MB,
            "max_images_per_message": config.MAX_IMAGES_PER_MESSAGE,
        },
    )


# ---------------------------------------------------------------------------
# 저장공간 (Storage)
#
# 관리자만 볼 수 있다. 용량 "확인" 만 제공하고 삭제 기능은 두지 않는다.
# 실수로 운영 데이터가 지워지는 것을 막기 위한 의도적인 제한이다.
# 계산은 storage.py 가 하고 짧게 캐시한다. ?refresh=1 이면 다시 계산한다.
# ---------------------------------------------------------------------------
@api.get("/storage")
@admin_required
def storage():
    db = get_db()
    refresh = (request.args.get("refresh") or "").strip() in ("1", "true", "yes")
    return jsonify(ok=True, storage=storage_module.report(db, refresh=refresh))


# ---------------------------------------------------------------------------
# Claude 설정
# ---------------------------------------------------------------------------
@api.get("/settings")
@admin_required
def get_settings():
    db = get_db()
    return jsonify(ok=True, settings=settings_store.public_view(db),
                   encrypted=settings_store.encryption_available())


@api.post("/settings")
@admin_required
def post_settings():
    db = get_db()
    data = request.get_json(silent=True) or {}
    updates = data.get("settings")
    if not isinstance(updates, dict):
        abort(400, "설정 값이 올바르지 않습니다.")
    try:
        changed = settings_store.save(db, updates, current_user()["id"])
    except settings_store.SettingError as exc:
        abort(400, str(exc))
    if changed:
        # 값은 절대 기록하지 않는다. 바뀐 항목 이름만 남긴다.
        audit(db, current_user()["id"], "claude_settings_changed", "settings", "",
              "keys=" + ",".join(sorted(changed)))
        db.commit()
    return jsonify(ok=True, changed=changed, settings=settings_store.public_view(db))


@api.post("/settings/<key>/clear")
@admin_required
def clear_setting(key):
    db = get_db()
    try:
        settings_store.clear_secret(db, key, current_user()["id"])
    except settings_store.SettingError as exc:
        abort(400, str(exc))
    audit(db, current_user()["id"], "claude_settings_changed", "settings", key, "cleared")
    db.commit()
    return jsonify(ok=True, settings=settings_store.public_view(db))


@api.post("/claude/test")
@admin_required
def claude_test():
    """실제로 짧은 요청을 보내 연결과 인증을 확인한다."""
    db = get_db()
    cfg = settings_store.snapshot(db)
    cfg["upload_dir"] = config.UPLOAD_DIR
    provider = providers.build(cfg)
    res = provider.test()
    audit(db, current_user()["id"], "claude_connection_tested", "settings", "",
          "provider=%s result=%s" % (cfg["provider"], "ok" if res["ok"] else "fail"))
    db.commit()
    return jsonify(ok=bool(res["ok"]), info=res.get("info"),
                   reply=res.get("reply"), error=res.get("error"))


# ---------------------------------------------------------------------------
# 사용자 관리
# ---------------------------------------------------------------------------
@api.get("/users")
@admin_required
def list_users():
    db = get_db()
    rows = db.execute(
        "SELECT u.*, (SELECT COUNT(*) FROM sessions s WHERE s.owner_id = u.id) AS session_count "
        "FROM users u ORDER BY u.id").fetchall()
    out = []
    for r in rows:
        d = public_user(r)
        d["session_count"] = r["session_count"]
        out.append(d)
    return jsonify(ok=True, users=out)


@api.post("/users")
@admin_required
def add_user():
    db = get_db()
    data = request.get_json(silent=True) or {}
    try:
        uid = create_user(
            db,
            data.get("username", ""),
            data.get("password", ""),
            data.get("display_name", ""),
            role=(data.get("role") or "user"),
            is_active=1 if data.get("is_active", True) else 0,
        )
    except ValueError as exc:
        abort(400, str(exc))
    audit(db, current_user()["id"], "user_created", "user", uid,
          "username=%s role=%s" % (data.get("username"), data.get("role") or "user"))
    db.commit()
    return jsonify(ok=True, user=public_user(user_by_id(db, uid))), 201


@api.patch("/users/<int:uid>")
@admin_required
def update_user(uid):
    db = get_db()
    me = current_user()
    row = user_by_id(db, uid)
    if row is None:
        abort(404, "사용자를 찾을 수 없습니다.")
    data = request.get_json(silent=True) or {}

    fields, values, actions = [], [], []

    if "username" in data:
        try:
            name = validate_username(data["username"])
        except ValueError as exc:
            abort(400, str(exc))
        other = db.execute("SELECT 1 FROM users WHERE username = ? AND id != ?",
                           (name, uid)).fetchone()
        if other:
            abort(400, "이미 존재하는 아이디입니다.")
        fields.append("username = ?")
        values.append(name)
        actions.append("username_changed")

    if "display_name" in data:
        fields.append("display_name = ?")
        values.append((data.get("display_name") or "").strip()[:100] or row["username"])

    if "role" in data:
        role = (data.get("role") or "user").strip()
        if role not in ("admin", "user"):
            abort(400, "역할이 올바르지 않습니다.")
        if uid == me["id"] and role != "admin":
            abort(400, "자기 자신의 관리자 권한은 해제할 수 없습니다.")
        if role != "admin" and row["role"] == "admin" and _last_admin(db, uid):
            abort(400, "마지막 관리자의 권한은 해제할 수 없습니다.")
        fields.append("role = ?")
        values.append(role)
        actions.append("role_changed")

    if "is_active" in data:
        active = 1 if data.get("is_active") else 0
        if uid == me["id"] and not active:
            abort(400, "자기 자신을 비활성화할 수 없습니다.")
        if not active and row["role"] == "admin" and _last_admin(db, uid):
            abort(400, "마지막 관리자는 비활성화할 수 없습니다.")
        fields.append("is_active = ?")
        values.append(active)
        actions.append("user_enabled" if active else "user_disabled")

    if not fields:
        abort(400, "변경할 내용이 없습니다.")

    fields.append("updated_at = ?")
    values += [ts(), uid]
    db.execute("UPDATE users SET %s WHERE id = ?" % ", ".join(fields), values)
    for action in actions or ["user_updated"]:
        audit(db, me["id"], action, "user", uid, "username=%s" % row["username"])
    db.commit()
    return jsonify(ok=True, user=public_user(user_by_id(db, uid)))


def _last_admin(db, uid):
    n = db.execute(
        "SELECT COUNT(*) FROM users WHERE role='admin' AND is_active=1 AND id != ?",
        (uid,)).fetchone()[0]
    return n == 0


@api.post("/users/<int:uid>/password")
@admin_required
def reset_password(uid):
    db = get_db()
    row = user_by_id(db, uid)
    if row is None:
        abort(404, "사용자를 찾을 수 없습니다.")
    data = request.get_json(silent=True) or {}
    try:
        set_password(db, uid, data.get("password", ""))
    except ValueError as exc:
        abort(400, str(exc))
    # 비밀번호 값은 audit 에 남기지 않는다.
    audit(db, current_user()["id"], "password_reset", "user", uid,
          "username=%s" % row["username"])
    db.commit()
    return jsonify(ok=True)


@api.delete("/users/<int:uid>")
@admin_required
def delete_user(uid):
    db = get_db()
    me = current_user()
    row = user_by_id(db, uid)
    if row is None:
        abort(404, "사용자를 찾을 수 없습니다.")
    if uid == me["id"]:
        abort(400, "자기 자신은 삭제할 수 없습니다.")
    if row["role"] == "admin" and _last_admin(db, uid):
        abort(400, "마지막 관리자는 삭제할 수 없습니다.")
    # 세션/메시지는 ON DELETE SET NULL 로 남는다. 대화가 사라지지 않게 하기 위함이다.
    db.execute("DELETE FROM users WHERE id = ?", (uid,))
    audit(db, me["id"], "user_deleted", "user", uid, "username=%s" % row["username"])
    db.commit()
    return jsonify(ok=True, deleted=uid)


# ---------------------------------------------------------------------------
# 세션 메타데이터 관리 (대화 내용은 제공하지 않는다)
# ---------------------------------------------------------------------------
@api.get("/sessions")
@admin_required
def admin_sessions():
    db = get_db()
    rows = db.execute(
        "SELECT s.id, s.name, s.visibility, s.owner_id, s.project_id, s.created_at,"
        "       s.updated_at, p.name AS project_name, u.username AS owner_username,"
        "       u.display_name AS owner_display_name,"
        "       (SELECT COUNT(*) FROM messages m WHERE m.session_id = s.id) AS message_count "
        "FROM sessions s JOIN projects p ON p.id = s.project_id "
        "LEFT JOIN users u ON u.id = s.owner_id ORDER BY s.id DESC LIMIT 500").fetchall()
    return jsonify(ok=True, sessions=[row_to_dict(r) for r in rows])


@api.patch("/sessions/<int:sid>")
@admin_required
def admin_update_session(sid):
    """
    레거시 세션(owner_id IS NULL)에 소유자를 지정하기 위한 기능.
    대화 내용은 건드리지 않는다.
    """
    db = get_db()
    row = db.execute("SELECT * FROM sessions WHERE id = ?", (sid,)).fetchone()
    if row is None:
        abort(404, "세션을 찾을 수 없습니다.")
    data = request.get_json(silent=True) or {}
    if "owner_id" not in data:
        abort(400, "변경할 내용이 없습니다.")
    owner_id = data.get("owner_id")
    if owner_id is not None:
        if user_by_id(db, owner_id) is None:
            abort(400, "존재하지 않는 사용자입니다.")
    db.execute("UPDATE sessions SET owner_id = ?, updated_at = ? WHERE id = ?",
               (owner_id, ts(), sid))
    audit(db, current_user()["id"], "session_owner_changed", "session", sid,
          "owner_id=%s" % owner_id)
    db.commit()
    return jsonify(ok=True)


@api.delete("/sessions/<int:sid>")
@admin_required
def admin_delete_session(sid):
    """시스템 관리 목적의 삭제. 일반 사용자 UI 의 삭제와는 별도 경로다."""
    db = get_db()
    row = db.execute("SELECT * FROM sessions WHERE id = ?", (sid,)).fetchone()
    if row is None:
        abort(404, "세션을 찾을 수 없습니다.")
    from app import remove_tree  # 순환 import 방지를 위해 지연 import

    db.execute("DELETE FROM sessions WHERE id = ?", (sid,))
    audit(db, current_user()["id"], "session_deleted", "session", sid,
          "admin=1 name=%s" % row["name"])
    db.commit()
    files_ok = remove_tree(os.path.join(
        config.UPLOAD_DIR, "project_%d" % row["project_id"], "session_%d" % sid))
    return jsonify(ok=True, deleted=sid, files_removed=files_ok)


# ---------------------------------------------------------------------------
# 감사 로그
# ---------------------------------------------------------------------------
@api.get("/audit")
@admin_required
def audit_log():
    db = get_db()
    limit = min(max(int(request.args.get("limit", 100) or 100), 1), 500)
    rows = db.execute(
        "SELECT a.*, u.username FROM audit_logs a LEFT JOIN users u ON u.id = a.user_id "
        "ORDER BY a.id DESC LIMIT ?", (limit,)).fetchall()
    return jsonify(ok=True, logs=[row_to_dict(r) for r in rows])
