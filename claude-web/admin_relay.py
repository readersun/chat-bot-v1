#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
admin_relay
===========

운영 묶음의 「중계 설정」. 세 탭이 한 화면에 있다.

    /admin/relay          연결과 등록   (붙어 있는 중계, 등록 코드, 설정값)
    /admin/relay/grants   사용 허용     (누가 어느 서버를 어느 등급으로)
    /admin/relay/log      기록         (누가 어디서 무엇을)

기록에 무엇을 넣고 무엇을 넣지 않는가
-------------------------------------
넣는다   : 시각, 사람, 서버, 어디서(채팅/터미널/설정), 무엇을(명령문), 등급, 끝(종료코드)
넣지 않는다 : 명령의 **출력**, 대화 본문, 비밀번호, 키

명령문을 넣는 이유는 그것이 회사 서버에 실제로 나간 행위이기 때문이다. 반대로
출력과 대화 본문은 그 대화를 쓰는 사람의 것이다. private 대화의 내용을 관리자가
기록 화면으로 돌아 읽을 수 있으면 "나만 보기" 가 거짓이 된다.

지우는 연산을 두지 않는다. 화면에도 API 에도 없다. 기록을 손댈 수 있으면
기록이 아니다. (전송 큐 relay_jobs 의 끝난 행만 보관 기간 뒤에 자동 정리된다)
"""

import csv
import io

from flask import Blueprint, Response, abort, jsonify, render_template, request

import config
import permissions as _perm
import relay_store as store
import tunnel_store
import settings_store
import ssh_policy
from auth import admin_required, csrf_token, current_user, public_user
from db import audit, get_db, row_to_dict, ts

bp = Blueprint("admin_relay", __name__, url_prefix="/admin/relay")
api = Blueprint("admin_relay_api", __name__, url_prefix="/api/admin/relay")

TABS = ("connect", "grants", "log")
TAB_TITLES = {"connect": "중계 설정", "grants": "사용 허용", "log": "기록"}

# 이 화면에서 다루는 설정 키. settings_store 의 다른 키는 여기서 바뀌지 않는다.
SETTING_KEYS = ("relay_policy", "relay_poll_seconds", "relay_run_timeout",
                "relay_approval_seconds", "relay_term_max_per_user",
                "relay_term_idle_seconds", "relay_chat_max_commands",
                "relay_queue_keep_days", "relay_tunnel_idle_seconds",
                "relay_tunnel_max_per_user")


def _page(tab):
    user = current_user()
    return render_template(
        "admin_relay.html",
        tab=tab,
        page_title=TAB_TITLES[tab],
        csrf=csrf_token(),
        me=public_user(user),
        is_admin=True,
        menus=list(_perm.MENUS),
        rail="relay",
    )


# 레일은 /admin/relay 로 건다. url_prefix 가 그 자체이므로 규칙을 빈 문자열과
# "/" 둘 다 달아 둔다. 하나만 달면 다른 쪽이 308 로 한 번 더 돌아간다.
@bp.get("")
@bp.get("/")
@admin_required
def home():
    return _page("connect")


@bp.get("/grants")
@admin_required
def grants_page():
    return _page("grants")


@bp.get("/log")
@admin_required
def log_page():
    return _page("log")


# ---------------------------------------------------------------------------
# 연결과 등록
# ---------------------------------------------------------------------------
@api.get("/summary")
@admin_required
def summary():
    db = get_db()
    agents = store.all_agents(db)
    live = {a["id"] for a in store.live_agents(db)}

    today = db.execute(
        "SELECT"
        " (SELECT COUNT(*) FROM ssh_commands WHERE created_at >= date('now','localtime')) AS cmds,"
        " (SELECT COUNT(*) FROM ssh_commands WHERE created_at >= date('now','localtime')"
        "    AND state IN ('done','failed') AND approved_by IS NOT NULL) AS approved,"
        " (SELECT COUNT(*) FROM term_sessions WHERE state IN ('opening','open')) AS terms_open,"
        " (SELECT COUNT(*) FROM ssh_servers) AS servers,"
        " (SELECT COUNT(*) FROM ssh_servers WHERE is_enabled = 1) AS servers_on"
    ).fetchone()

    settings = {k: settings_store.get(db, k, "") for k in SETTING_KEYS}
    prog = store.program_info()
    cprog = store.program_info("client")
    return jsonify(
        ok=True,
        connected=bool(live),
        agents=[{"id": a["id"], "owner": (a["display_name"] or a["username"]
                                          or "(주인 없음)"),
                 "owner_id": a["owner_id"],
                 "name": a["name"], "version": a["version"], "os": a["os_info"],
                 "ip": a["ip"], "scheme": a["scheme"],
                 "registered_at": a["registered_at"],
                 "last_seen_at": a["last_seen_at"],
                 "revoked_at": a["revoked_at"],
                 "live": a["id"] in live} for a in agents],
        program=({"name": prog["name"], "size": prog["size"],
                  "sha256": prog["sha256"], "uploaded_at": prog["uploaded_at"],
                  "uploaded_by": prog["uploaded_by"]} if prog else None),
        client_program=({"name": cprog["name"], "size": cprog["size"],
                         "sha256": cprog["sha256"], "uploaded_at": cprog["uploaded_at"],
                         "uploaded_by": cprog["uploaded_by"]} if cprog else None),
        program_max_mb=config.RELAY_PROGRAM_MAX_MB,
        counts=row_to_dict(today),
        settings=settings,
        encrypted=settings_store.encryption_available(),
        # 비밀번호 인증을 쓰는 서버가 있는데 중계가 http 로 붙어 있으면 알려 준다.
        insecure_warning=_insecure_warning(db, agents),
        plaintext_secrets=_plaintext_secret_count(db),
        server_url=request.url_root.rstrip("/"))


def _plaintext_secret_count(db):
    """
    암호화되지 않은 채 들어 있는 서버 비밀번호의 개수.

    지금은 암호화할 수 없으면 저장을 거부한다(relay._secret_for_db). 하지만
    그렇게 고치기 전에, 또는 cryptography 없이 돌던 서버에 들어간 값은 평문
    그대로 남아 있다. 새로 저장할 때만 막으면 남은 것은 영원히 평문이므로,
    몇 건인지 세어서 다시 저장하라고 알려 준다. 값은 세기만 하고 읽지 않는다.
    """
    return db.execute(
        "SELECT COUNT(*) AS c FROM ssh_servers"
        " WHERE secret_enc <> '' AND secret_enc NOT LIKE 'enc:v1:%'"
    ).fetchone()["c"]


def _insecure_warning(db, agents):
    """
    평문 http 로 붙은 중계가 하나라도 있고, 비밀번호로 접속하는 서버가 있으면
    알려 준다. 그 조합에서는 접속 정보가 사내망을 평문으로 지나간다.
    """
    plain = [a for a in agents
             if a["revoked_at"] is None and (a["scheme"] or "").lower() != "https"]
    if not plain:
        return ""
    n = db.execute("SELECT COUNT(*) AS c FROM ssh_servers"
                   " WHERE auth_kind = 'password' AND is_enabled = 1").fetchone()["c"]
    if not n:
        return ""
    return ("중계 %d대가 평문 http 로 붙어 있고, 비밀번호로 접속하는 서버가 %d대 "
            "있습니다. 접속 정보가 사내망을 평문으로 지나갑니다. https 로 바꾸거나 "
            "키 인증으로 바꾸세요." % (len(plain), n))


def _program_kind():
    """?kind=relay|client. 없으면 relay (예전 화면이 그대로 동작한다)."""
    kind = (request.args.get("kind") or "relay").strip().lower()
    if kind not in store.PROGRAM_KINDS:
        abort(400, "프로그램 종류가 올바르지 않습니다.")
    return kind


@api.post("/program")
@admin_required
def upload_program():
    """
    중계 프로그램(relay.exe)을 올린다. 관리자만 올릴 수 있다.

    여기 올린 파일은 **사람들이 받아서 자기 PC 에서 실행한다.** 아무나 올릴 수
    있으면 그 자체가 사내에 프로그램을 뿌리는 길이 된다. 그래서 관리자만이고,
    받는 화면에는 sha256 을 함께 보여 준다.
    """
    db, user = get_db(), current_user()
    kind = _program_kind()
    f = request.files.get("file")
    if f is None or not f.filename:
        abort(400, "파일을 고르세요.")
    try:
        meta = store.save_program(f.stream, f.filename, user["id"],
                                  user["display_name"] or user["username"], kind=kind)
    except ValueError as exc:
        abort(400, str(exc))
    audit(db, user["id"], "%s_program_uploaded" % kind, kind, "",
          "name=%s size=%d sha256=%s" % (meta["name"], meta["size"],
                                         meta["sha256"][:12]))
    db.commit()
    return jsonify(ok=True, program={"name": meta["name"], "size": meta["size"],
                                     "sha256": meta["sha256"],
                                     "uploaded_at": meta["uploaded_at"],
                                     "uploaded_by": meta["uploaded_by"]})


@api.delete("/program")
@admin_required
def delete_program():
    """올려 둔 프로그램을 내린다. 받는 길이 닫힌다."""
    db, user = get_db(), current_user()
    kind = _program_kind()
    if not store.remove_program(kind):
        abort(404, "올라와 있는 프로그램이 없습니다.")
    audit(db, user["id"], "%s_program_removed" % kind, kind, "", "")
    db.commit()
    return jsonify(ok=True)


@api.post("/revoke")
@admin_required
def revoke():
    """중계를 끊는다. 열려 있던 터미널도 함께 닫는다."""
    db, user = get_db(), current_user()
    data = request.get_json(silent=True) or {}
    try:
        agent_id = int(data.get("agent_id") or 0)
    except (TypeError, ValueError):
        abort(400, "중계를 고르세요.")
    row = db.execute("SELECT * FROM relay_agents WHERE id = ?", (agent_id,)).fetchone()
    if row is None:
        abort(404, "그런 중계가 없습니다.")
    store.revoke_agent(db, agent_id)

    # 끊은 중계의 주인이 열어 둔 터미널만 닫는다. 남의 터미널은 건드리지 않는다.
    closed = (store.close_user_terms(db, row["owner_id"],
                                     "관리자가 중계 연결을 끊었습니다")
              if row["owner_id"] else 0)
    if row["owner_id"]:
        tunnel_store.close_user_tunnels(db, row["owner_id"],
                                        "관리자가 VDI 중계 연결을 끊어 터널을 닫았습니다")
    audit(db, user["id"], "relay_revoked", "relay", agent_id,
          "name=%s owner=%s terms_closed=%d"
          % (row["name"], row["owner_id"], closed))
    db.commit()
    store.wake()
    return jsonify(ok=True, revoked=agent_id, terminals_closed=closed)


@api.post("/settings")
@admin_required
def save_settings():
    db, user = get_db(), current_user()
    data = request.get_json(silent=True) or {}
    updates = {k: v for k, v in data.items() if k in SETTING_KEYS}
    if not updates:
        abort(400, "변경할 내용이 없습니다.")
    try:
        changed = settings_store.save(db, updates, user["id"])
    except settings_store.SettingError as exc:
        abort(400, str(exc))
    if changed:
        audit(db, user["id"], "relay_settings_changed", "settings", "",
              "keys=%s" % ",".join(sorted(changed)))
        db.commit()
    return jsonify(ok=True, changed=changed,
                   settings={k: settings_store.get(db, k, "") for k in SETTING_KEYS})


# ---------------------------------------------------------------------------
# 사용 허용
# ---------------------------------------------------------------------------
@api.get("/grants")
@admin_required
def list_grants():
    db = get_db()
    servers = db.execute(
        "SELECT id, name, host, is_enabled FROM ssh_servers ORDER BY name").fetchall()
    users = db.execute(
        "SELECT id, username, display_name, role, is_active, ssh_level, ssh_all_servers,"
        " ssh_shell"
        " FROM users ORDER BY role != 'admin', username").fetchall()
    rows = db.execute("SELECT user_id, server_id FROM ssh_grants").fetchall()
    by_user = {}
    for r in rows:
        by_user.setdefault(r["user_id"], []).append(r["server_id"])

    menu_rows = db.execute(
        "SELECT user_id FROM user_menus WHERE menu_key = 'servers'").fetchall()
    has_menu = {r["user_id"] for r in menu_rows}

    out = []
    for u in users:
        is_admin = u["role"] == "admin"
        out.append({
            "id": u["id"], "username": u["username"],
            "display_name": u["display_name"] or u["username"],
            "role": u["role"], "is_active": bool(u["is_active"]),
            "level": _perm.ssh_level(db, u) if is_admin else (u["ssh_level"] or "off"),
            "all_servers": True if is_admin else bool(u["ssh_all_servers"]),
            "shell": _perm.shell_level(db, u),
            "server_ids": sorted(by_user.get(u["id"], [])),
            "has_menu": is_admin or u["id"] in has_menu,
            # 관리자는 이 표를 스스로 고칠 수 있다. 줄은 보여 주되 잠근다.
            "locked": is_admin,
        })
    return jsonify(ok=True, users=out,
                   servers=[row_to_dict(s) for s in servers],
                   policy=_perm.ssh_policy(db),
                   levels=[{"key": k, "label": _perm.SSH_LEVEL_LABELS[k]}
                           for k in _perm.SSH_LEVELS],
                   shells=[{"key": k, "label": _perm.SHELL_LEVEL_LABELS[k]}
                           for k in _perm.SHELL_LEVELS])


@api.put("/grants/<int:uid>")
@admin_required
def put_grants(uid):
    """
    한 사람의 등급과 범위를 바꾼다.

    메뉴도 같이 움직인다. 등급을 주면서 「서버」 메뉴를 안 주면 그 사람 화면에는
    아무것도 안 생기고, 메뉴만 주고 등급을 안 주면 들어가서 빈 목록을 본다.
    둘을 따로 두면 그 조합을 반드시 누군가 만든다.
    """
    db, user = get_db(), current_user()
    row = db.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()
    if row is None:
        abort(404, "그런 사용자가 없습니다.")
    if row["role"] == "admin":
        abort(400, "관리자는 사용 허용 표로 막지 않습니다. 관리자는 이 표를 "
                   "스스로 고칠 수 있기 때문입니다. 기록에는 관리자가 한 일도 "
                   "그대로 남습니다.")

    data = request.get_json(silent=True) or {}
    level = (data.get("level") or "off").strip().lower()
    if level not in _perm.SSH_LEVELS:
        abort(400, "등급이 올바르지 않습니다.")
    all_servers = bool(data.get("all_servers"))
    ids = data.get("server_ids") or []
    if not isinstance(ids, list):
        abort(400, "서버 목록이 올바르지 않습니다.")
    try:
        ids = [int(x) for x in ids]
    except (TypeError, ValueError):
        abort(400, "서버 목록이 올바르지 않습니다.")
    if level != "off" and not all_servers and not ids:
        abort(400, "범위를 '고른 서버만' 으로 두면 서버를 하나 이상 골라야 합니다.")
    shell = data.get("shell")
    if shell is not None:
        shell = (shell or "off").strip().lower()
        if shell not in _perm.SHELL_LEVELS:
            abort(400, "셸 열기 값이 올바르지 않습니다.")

    before_shell = _perm.shell_level(db, row)
    before_ids = _perm.allowed_server_ids(db, row)
    lv, keep = _perm.set_user_ssh(db, uid, level, all_servers, ids, user["id"],
                                  shell=shell)
    after = db.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()
    after_shell = _perm.shell_level(db, after)

    menus = set(_perm.user_menus(db, row))
    if lv == "off":
        menus.discard("servers")
    else:
        menus.add("servers")
    _perm.set_user_menus(db, uid, menus, user["id"])

    # 줄어든 것이 있으면 그 자리에서 닫는다. 다음 청소까지 기다리면 허용이
    # 사라진 뒤에도 셸이 몇 초 살아 있다. 범위가 줄어든 경우도 같다
    # (어느 서버가 빠졌는지 따로 가리지 않고 그 사람의 셸을 모두 닫는다).
    narrowed = (before_ids is None and not (all_servers and lv != "off")) or (
        before_ids is not None and not set(before_ids) <= set(keep)
        and not all_servers)
    closed = tunnels = 0
    reason = "사용 허용이 바뀌어 닫았습니다"
    if lv == "off" or after_shell == _perm.SHELL_OFF or narrowed:
        closed = store.close_user_terms(db, uid, reason)
    if lv == "off" or after_shell != _perm.SHELL_TUNNEL or narrowed:
        tunnels = tunnel_store.close_user_tunnels(db, uid, reason)

    audit(db, user["id"], "ssh_grants_changed", "user", uid,
          "level=%s shell=%s->%s all=%s servers=%s terms_closed=%d tunnels_closed=%d"
          % (lv, before_shell, after_shell, all_servers,
             ",".join(str(x) for x in keep) or "-", closed, tunnels))
    db.commit()
    store.wake()
    return jsonify(ok=True, level=lv, shell=after_shell, all_servers=all_servers,
                   server_ids=keep, terminals_closed=closed, tunnels_closed=tunnels,
                   menus=sorted(menus))


# ---------------------------------------------------------------------------
# 기록
# ---------------------------------------------------------------------------
SOURCE_LABELS = {"chat": "채팅", "term": "터미널", "setup": "설정"}

_TUNNEL_STATE_LABELS = {"open": "열려 있음", "opening": "여는 중", "closed": "닫힘",
                        "failed": "열지 못함"}

# 기록에 함께 싣는 운영 행위. 값은 화면에 쓸 문장이다.
_AUDIT_ACTIONS = {
    "ssh_server_added": "서버 등록",
    "ssh_server_changed": "서버 수정",
    "ssh_server_removed": "서버 삭제",
    "ssh_server_test": "연결 테스트",
    "relay_registered": "중계 등록",
    "relay_revoked": "중계 해제",
    "relay_enroll_code_issued": "등록 코드 발급",
    "relay_register_failed": "중계 등록 실패",
    "ssh_grants_changed": "사용 허용 변경",
    "relay_settings_changed": "중계 설정 변경",
    "client_registered": "클라이언트 등록",
    "client_revoked": "클라이언트 해제",
    "client_enroll_code_issued": "클라이언트 등록 코드 발급",
    "client_register_failed": "클라이언트 등록 실패",
}


def _bytes_text(n):
    n = int(n or 0)
    if n < 1024:
        return "%d B" % n
    if n < 1048576:
        return "%.1f KB" % (n / 1024.0)
    return "%.1f MB" % (n / 1048576.0)


def _filters():
    args = request.args
    days = args.get("days") or "7"
    try:
        days = max(1, min(365, int(days)))
    except (TypeError, ValueError):
        days = 7
    try:
        user_id = int(args.get("user_id") or 0) or None
    except (TypeError, ValueError):
        user_id = None
    try:
        server_id = int(args.get("server_id") or 0) or None
    except (TypeError, ValueError):
        server_id = None
    kind = (args.get("kind") or "all").lower()
    if kind not in ("all", "read", "write", "term", "tunnel", "setup"):
        kind = "all"
    return days, user_id, server_id, kind


def _log_rows(db, days, user_id, server_id, kind, limit=300):
    """
    세 곳에서 모아 시간 역순으로 합친다.

        ssh_commands  챗봇이 고른 명령 (등급과 승인)
        term_sessions 사람이 직접 쓴 터미널 (세션 하나가 한 줄)
        audit_logs    서버/중계/허용 설정을 바꾼 일

    한 줄로 합치는 이유: "그 시간에 그 서버에서 무슨 일이 있었나" 를 보려면
    세 화면을 번갈아 보면 안 된다.
    """
    since = "datetime('now','localtime','-%d days')" % days
    out = []

    if kind in ("all", "read", "write"):
        sql = ("SELECT c.*, u.username, u.display_name, s.name AS server_name,"
               " au.username AS approver_username, au.display_name AS approver_display"
               " FROM ssh_commands c"
               " LEFT JOIN users u ON u.id = c.user_id"
               " LEFT JOIN users au ON au.id = c.approved_by"
               " LEFT JOIN ssh_servers s ON s.id = c.server_id"
               " WHERE c.created_at >= " + since)
        params = []
        if user_id:
            sql += " AND c.user_id = ?"
            params.append(user_id)
        if server_id:
            sql += " AND c.server_id = ?"
            params.append(server_id)
        if kind in ("read", "write"):
            sql += " AND c.level = ?"
            params.append(kind)
        sql += " ORDER BY c.id DESC LIMIT ?"
        params.append(limit)
        for r in db.execute(sql, params):
            out.append({
                "at": r["created_at"],
                "kind": "command",
                "who": r["display_name"] or r["username"] or "(삭제된 사용자)",
                "server": r["server_name"] or "(삭제된 서버)",
                "where": SOURCE_LABELS.get(r["source"], r["source"]),
                "what": r["command"],
                "level": r["level"],
                "level_label": ssh_policy.LEVEL_LABELS.get(r["level"], r["level"]),
                "state": r["state"],
                "state_label": _STATE_LABELS.get(r["state"], r["state"]),
                "end": _end_text(r),
                "approver": (r["approver_display"] or r["approver_username"] or ""),
                "approved_at": r["approved_at"],
                "reason": r["reason"],
                "ref": r["id"],
            })

    if kind in ("all", "term"):
        sql = ("SELECT t.*, u.username, u.display_name, s.name AS server_name"
               " FROM term_sessions t"
               " LEFT JOIN users u ON u.id = t.user_id"
               " LEFT JOIN ssh_servers s ON s.id = t.server_id"
               " WHERE t.opened_at >= " + since)
        params = []
        if user_id:
            sql += " AND t.user_id = ?"
            params.append(user_id)
        if server_id:
            sql += " AND t.server_id = ?"
            params.append(server_id)
        sql += " ORDER BY t.opened_at DESC LIMIT ?"
        params.append(limit)
        for r in db.execute(sql, params):
            out.append({
                "at": r["opened_at"],
                "kind": "term",
                "who": r["display_name"] or r["username"] or "(삭제된 사용자)",
                "server": r["server_name"] or "(삭제된 서버)",
                "where": "터미널",
                "what": "세션 1개 · %s · 입력 %d줄" % (
                    _duration(r["opened_at"], r["closed_at"]), r["lines_in"]),
                "level": "human",
                "level_label": "사람이 직접",
                "state": r["state"],
                "state_label": {"open": "열려 있음", "opening": "열고 있음",
                                "closed": "닫힘", "failed": "실패"}.get(r["state"],
                                                                      r["state"]),
                "end": r["close_reason"] or "",
                "approver": "",
                "approved_at": None,
                "reason": "",
                "ref": r["id"],
            })

    if kind in ("all", "tunnel"):
        # PuTTY 터널. 암호문만 지나가므로 **무엇을 쳤는지는 여기에도 어디에도 없다.**
        # 누가 · 언제 · 어느 서버 · 얼마나 · 몇 바이트 · 어디서(클라이언트 IP)만.
        sql = ("SELECT t.*, u.username, u.display_name, s.name AS server_name"
               " FROM tunnel_sessions t"
               " LEFT JOIN users u ON u.id = t.user_id"
               " LEFT JOIN ssh_servers s ON s.id = t.server_id"
               " WHERE t.opened_at >= " + since)
        params = []
        if user_id:
            sql += " AND t.user_id = ?"
            params.append(user_id)
        if server_id:
            sql += " AND t.server_id = ?"
            params.append(server_id)
        sql += " ORDER BY t.opened_at DESC LIMIT ?"
        params.append(limit)
        for r in db.execute(sql, params):
            out.append({
                "at": r["opened_at"],
                "kind": "tunnel",
                "who": r["display_name"] or r["username"] or "(삭제된 사용자)",
                "server": r["server_name"] or "(삭제된 서버)",
                "where": "PuTTY 터널",
                "what": "%s · 올림 %s · 내림 %s · %s" % (
                    _duration(r["opened_at"], r["closed_at"]),
                    _bytes_text(r["bytes_up"]), _bytes_text(r["bytes_down"]),
                    r["client_ip"] or "-"),
                "level": "human",
                "level_label": "사람이 직접",
                "state": r["state"],
                "state_label": _TUNNEL_STATE_LABELS.get(r["state"], r["state"]),
                "end": r["close_reason"] or "",
                "approver": "",
                "approved_at": None,
                "reason": "",
                "ref": r["id"],
            })

    if kind in ("all", "setup"):
        marks = ",".join("?" for _ in _AUDIT_ACTIONS)
        sql = ("SELECT a.*, u.username, u.display_name FROM audit_logs a"
               " LEFT JOIN users u ON u.id = a.user_id"
               " WHERE a.created_at >= " + since +
               " AND a.action IN (%s)" % marks)
        params = list(_AUDIT_ACTIONS)
        if user_id:
            sql += " AND a.user_id = ?"
            params.append(user_id)
        sql += " ORDER BY a.id DESC LIMIT ?"
        params.append(limit)
        for r in db.execute(sql, params):
            server_name = ""
            if r["target_type"] == "ssh_server" and r["target_id"]:
                srv = db.execute("SELECT name FROM ssh_servers WHERE id = ?",
                                 (r["target_id"],)).fetchone()
                server_name = srv["name"] if srv else ""
                if not server_name:
                    for piece in (r["details"] or "").split():
                        if piece.startswith("name="):
                            server_name = piece[5:]
            if server_id and not server_name:
                continue
            out.append({
                "at": r["created_at"],
                "kind": "setup",
                "who": r["display_name"] or r["username"] or "(시스템)",
                "server": server_name,
                "where": "설정",
                "what": _AUDIT_ACTIONS[r["action"]],
                "level": "setup",
                "level_label": "설정",
                "state": "",
                "state_label": "",
                "end": r["details"] or "",
                "approver": "",
                "approved_at": None,
                "reason": "",
                "ref": r["id"],
            })

    out.sort(key=lambda x: (x["at"], x["kind"]), reverse=True)
    return out[:limit]


_STATE_LABELS = {
    "pending": "승인 대기",
    "running": "실행 중",
    "done": "실행됨",
    "failed": "실패",
    "rejected": "거절",
    "expired": "시간 초과로 취소",
    "blocked": "차단",
    "denied": "권한 없음",
}


def _end_text(row):
    if row["state"] == "done":
        return "종료 %s · %s" % (
            "?" if row["exit_code"] is None else row["exit_code"],
            "%.1f초" % (row["duration_ms"] / 1000.0) if row["duration_ms"] else "-")
    if row["state"] in ("failed",):
        return row["reason"] or "실패"
    return _STATE_LABELS.get(row["state"], row["state"])


def _duration(start, end):
    import time as _t
    if not start:
        return "-"
    try:
        a = _t.mktime(_t.strptime(start, "%Y-%m-%d %H:%M:%S"))
        b = (_t.mktime(_t.strptime(end, "%Y-%m-%d %H:%M:%S")) if end else _t.time())
    except (TypeError, ValueError):
        return "-"
    secs = max(0, int(b - a))
    if secs < 60:
        return "%d초" % secs
    return "%d분 %d초" % (secs // 60, secs % 60)


@api.get("/log")
@admin_required
def log():
    db = get_db()
    days, user_id, server_id, kind = _filters()
    rows = _log_rows(db, days, user_id, server_id, kind)
    users = db.execute(
        "SELECT DISTINCT u.id, u.username, u.display_name FROM users u"
        " WHERE EXISTS (SELECT 1 FROM ssh_commands c WHERE c.user_id = u.id)"
        "    OR EXISTS (SELECT 1 FROM term_sessions t WHERE t.user_id = u.id)"
        " ORDER BY u.username").fetchall()
    servers = db.execute("SELECT id, name FROM ssh_servers ORDER BY name").fetchall()
    return jsonify(ok=True, rows=rows,
                   filters={"days": days, "user_id": user_id,
                            "server_id": server_id, "kind": kind},
                   users=[{"id": u["id"],
                           "name": u["display_name"] or u["username"]} for u in users],
                   servers=[row_to_dict(s) for s in servers],
                   keep_days=store.queue_keep_days(db))


@api.get("/log/term/<term_id>")
@admin_required
def term_lines(term_id):
    """
    터미널 한 세션에서 사람이 친 줄. (「펼쳐 보기」)

    등급은 매기지 않는다. 사람이 자기 계정 권한으로 한 일이다. 비밀번호를
    묻는 프롬프트 뒤에 온 줄은 '(가려짐)' 으로 적혀 있다.
    """
    db = get_db()
    row = store.term_row(db, term_id)
    if row is None:
        abort(404, "그런 터미널 기록이 없습니다.")
    lines = db.execute(
        "SELECT line, created_at FROM term_inputs WHERE term_id = ? ORDER BY id",
        (term_id,)).fetchall()
    srv = store.get_server(db, row["server_id"])
    who = db.execute("SELECT username, display_name FROM users WHERE id = ?",
                     (row["user_id"],)).fetchone()
    return jsonify(ok=True,
                   term={"id": row["id"], "state": row["state"],
                         "opened_at": row["opened_at"], "closed_at": row["closed_at"],
                         "close_reason": row["close_reason"],
                         "server": srv["name"] if srv else "",
                         "who": (who["display_name"] or who["username"]) if who else "",
                         "duration": _duration(row["opened_at"], row["closed_at"])},
                   lines=[row_to_dict(x) for x in lines])


@api.get("/log.csv")
@admin_required
def log_csv():
    """
    같은 내용을 CSV 로 받는다. 보관하려는 쪽(감사/보안)에서 쓴다.

    엑셀이 한글을 깨뜨리지 않도록 BOM 을 붙인다.
    """
    db, user = get_db(), current_user()
    days, user_id, server_id, kind = _filters()
    rows = _log_rows(db, days, user_id, server_id, kind, limit=5000)

    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(["시각", "사람", "서버", "어디서", "무엇을", "등급", "상태",
                "끝", "승인한 사람", "승인 시각"])
    for r in rows:
        w.writerow([r["at"], r["who"], r["server"], r["where"], r["what"],
                    r["level_label"], r["state_label"], r["end"],
                    r["approver"], r["approved_at"] or ""])
    audit(db, user["id"], "relay_log_exported", "relay", "",
          "days=%d rows=%d" % (days, len(rows)))
    db.commit()

    name = "relay-log-%s.csv" % ts().replace(" ", "_").replace(":", "")
    return Response(
        "﻿" + buf.getvalue(),
        mimetype="text/csv; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=\"%s\"" % name,
                 "Cache-Control": "no-store"})
