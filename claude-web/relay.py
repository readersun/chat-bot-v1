#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
relay
=====

SSH 중계의 화면과 API.

세 묶음이 한 파일에 있다.

    bp        /servers                사람이 보는 화면
    api       /api/servers, /api/term 브라우저가 부르는 API
    relay_api /api/relay/*            VDI 의 중계 프로그램이 부르는 API

통신 방향
---------
중계가 챗봇 서버로 **들어오는 방향 하나만** 쓴다. 챗봇 서버가 VDI 로 거는
연결은 없다. 그래서 방화벽에 구멍을 내지 않는다.

    중계 --POST /api/relay/poll--> 챗봇  (할 일 있으면 바로, 없으면 25초 기다림)
    중계 --POST /api/relay/result-> 챗봇  (결과와 터미널 화면)
    중계 --POST /api/relay/beat--> 챗봇  (터미널이 열려 있는 동안 5초마다)

왜 폴링인가
-----------
지금 배포는 WebSocket 을 받지 못한다. nginx 가 업그레이드 요청을 지우고
(docker/nginx.conf:92 의 `proxy_set_header Connection "";`) gunicorn 의 워커
종류가 받지 못한다(deploy/gunicorn.conf.py:48 의 `worker_class = "gthread"`).
그래서 브라우저는 0.25초마다 짧게 묻고, 중계만 25초짜리 긴 대기를 쓴다.
배포 설정은 한 줄도 바꾸지 않는다. 뒤에 실시간으로 바꿀 때 화면과 중계는
그대로 두고 전송만 갈아 끼운다.

긴 대기를 25초로 둔 이유: 스레드가 8개다(threads = 8). 중계 하나가 120초를
잡고 있으면 서버 처리 능력의 1/8 이 그 한 대 때문에 묶인다.
"""

import json
import re
import threading
import time
import uuid

from flask import (
    Blueprint, Response, abort, jsonify, render_template, request, send_file,
)

import auth
import config
import permissions
import relay_store as store
import settings_store
import ssh_policy
import tunnel_store as tstore
from db import audit, get_db, ts

bp = Blueprint("servers", __name__)
api = Blueprint("servers_api", __name__, url_prefix="/api")
relay_api = Blueprint("relay_api", __name__, url_prefix="/api/relay")

# 중계 API 는 쿠키를 쓰지 않는다. before_request 의 CSRF 검사를 건너뛸 경로를
# app.py 가 이 접두사로 판단한다. (헤더로만 인증하므로 브라우저가 흉내낼 수 없다)
#
# **이 접두사 아래에 브라우저가 부르는 길을 두면 안 된다.** 그 길은 쿠키로
# 인증되는데 CSRF 검사를 건너뛰게 되어, 남의 사이트가 사용자 몰래 부를 수 있다.
# 그래서 "내 중계" 화면이 쓰는 길은 /api/my-relay/* 로 따로 둔다.
NO_CSRF_PREFIX = "/api/relay/"


# ---------------------------------------------------------------------------
# 공통
# ---------------------------------------------------------------------------
def _me():
    return auth.current_user()


def _server_or_403(db, server_id):
    row = store.get_server(db, server_id)
    permissions.require_server(db, _me(), row)
    return row


def _require_my_relay(db, user):
    """
    **내 중계**가 붙어 있어야 한다.

    사람마다 자기 VDI 에 중계를 깔고 자기 키로 붙는다. 남의 중계가 붙어 있어도
    내 일을 대신 해 주지 않는다. 그래서 "중계가 붙어 있지 않습니다" 가 아니라
    "내 VDI 의 중계가 붙어 있지 않습니다" 라고 말해야 한다.
    """
    agent = store.live_agent(db, user["id"])
    if agent is None:
        abort(503, "내 VDI 의 중계 프로그램이 붙어 있지 않습니다. "
                   "서버 화면의 「내 중계」에서 프로그램을 받아 설치하고 "
                   "등록 코드를 넣으세요.")
    return agent


def _client_scheme():
    fwd = request.headers.get("X-Forwarded-Proto", "")
    if config.TRUST_PROXY and fwd:
        return fwd.split(",")[0].strip()[:10]
    return request.scheme


def _my_relay_payload(db, user, agent=None):
    """
    「내 중계」 칸이 쓰는 값.

    사람마다 자기 VDI 에 중계를 깐다. 그래서 이 값은 **내 것**만 본다.
    프로그램을 아직 올려 두지 않았으면 받을 수 있는지(available)도 함께 알려
    준다. 받을 것이 없는데 "받으세요" 라고 쓰면 사람을 헛돌게 한다.
    """
    if agent is None:
        agent = store.live_agent(db, user["id"])
    prog = store.program_info()
    code = store.active_enroll_code(db, user["id"])
    return {
        "connected": agent is not None,
        "name": agent["name"] if agent else "",
        "version": agent["version"] if agent else "",
        "last_seen_at": agent["last_seen_at"] if agent else None,
        "program": {"available": bool(prog), "name": prog.get("name") if prog else "",
                    "size": prog.get("size") if prog else 0,
                    "sha256": prog.get("sha256") if prog else "",
                    "uploaded_at": prog.get("uploaded_at") if prog else ""},
        "enroll": ({"expires_at": code["expires_at"]} if code else None),
        "server_url": request.url_root.rstrip("/"),
    }


# ---------------------------------------------------------------------------
# 화면
# ---------------------------------------------------------------------------
@bp.get("/servers")
@auth.menu_required("servers")
def page():
    db, user = get_db(), _me()
    return render_template(
        "servers.html",
        csrf=auth.csrf_token(),
        me=auth.public_user(user),
        is_admin=permissions.is_admin(user),
        menus=sorted(permissions.user_menus(db, user)),
        rail="servers",
        ssh_level=permissions.ssh_level(db, user),
        term_max=store.term_max_per_user(db),
        # 「채팅」 단추는 채팅 메뉴를 받은 사람에게만 뜻이 있다. 없는 사람이
        # 누르면 /api/sessions 가 403 이다. 누를 수 없는 단추에는 이유를 붙인다.
        has_chat=permissions.has_menu(db, user, "chat"),
    )


@bp.get("/servers/<int:server_id>/terminal")
@auth.menu_required("servers")
def terminal_page(server_id):
    """
    웹 터미널.

    좁은 화면에서는 열지 않는다. 키보드가 화면 절반을 덮고 한 줄이 잘려
    보이는 상태로 명령을 치는 것이 더 위험하다. 막는 자리는 화면 폭을 아는
    브라우저 쪽이고, 여기서는 그 안내만 함께 넘긴다.
    """
    db, user = get_db(), _me()
    row = _server_or_403(db, server_id)
    permissions.require_shell(db, user, permissions.SHELL_CONSOLE)
    return render_template(
        "terminal.html",
        csrf=auth.csrf_token(),
        me=auth.public_user(user),
        is_admin=permissions.is_admin(user),
        menus=sorted(permissions.user_menus(db, user)),
        rail="servers",
        server=store.server_payload(row),
        term_max=store.term_max_per_user(db),
        max_input=config.RELAY_TERM_MAX_INPUT_BYTES,
    )


# ---------------------------------------------------------------------------
# 서버 목록 / 등록
# ---------------------------------------------------------------------------
@api.get("/servers")
@auth.menu_required("servers")
def list_servers():
    """
    쓸 수 있는 서버만 나온다. 거르는 자리는 SQL 이다.

    관리자는 꺼 둔 서버도 함께 본다. 관리자 화면이 따로 있는 패치와 달리
    서버는 이 화면 하나로 운영하기 때문이다. 대신 꺼진 서버는 아무 동작도
    열어 주지 않는다.
    """
    db, user = get_db(), _me()
    where, params = permissions.visible_servers_clause(db, user, "s")
    is_admin = permissions.is_admin(user)
    sql = "SELECT s.* FROM ssh_servers s WHERE " + where
    if not is_admin:
        sql += " AND s.is_enabled = 1"
    sql += " ORDER BY s.name"
    rows = db.execute(sql, params).fetchall()

    agent = store.live_agent(db, user["id"])
    opens = {}
    for r in store.open_terms_for_user(db, user["id"]):
        opens.setdefault(r["server_id"], []).append(
            {"term_id": r["id"], "state": r["state"], "opened_at": r["opened_at"]})

    out = []
    for r in rows:
        d = store.server_payload(r, with_admin=is_admin)
        d["terminals"] = opens.get(r["id"], [])
        out.append(d)
    return jsonify(
        ok=True,
        servers=out,
        level=permissions.ssh_level(db, user),
        shell=permissions.shell_level(db, user),
        policy=permissions.ssh_policy(db),
        can_manage=is_admin,
        relay=_my_relay_payload(db, user, agent),
        term_open=store.open_term_count(db, user["id"]),
        term_max=store.term_max_per_user(db))


def _read_server_form(data, for_update=False):
    """
    서버 한 대의 입력값을 검사한다. 반환: (fields dict, 비밀번호 raw 또는 None)

    비밀번호는 빈 문자열이면 '변경하지 않음' 이다. (화면에 원문이 없으므로)
    """
    out = {}
    name = (data.get("name") or "").strip()
    host = (data.get("host") or "").strip()
    username = (data.get("username") or "").strip()
    if not for_update or "name" in data:
        if not name:
            abort(400, "서버 이름을 입력해 주세요.")
        if not re.match(r"^[A-Za-z0-9가-힣][A-Za-z0-9가-힣._-]{0,49}$", name):
            abort(400, "서버 이름은 영문/숫자/한글과 . _ - 만 쓸 수 있습니다. (최대 50자)")
        out["name"] = name
    if not for_update or "host" in data:
        if not host:
            abort(400, "주소를 입력해 주세요.")
        if not re.match(r"^[A-Za-z0-9._:-]{1,120}$", host):
            abort(400, "주소에 쓸 수 없는 문자가 있습니다.")
        out["host"] = host
    if not for_update or "port" in data:
        try:
            port = int(data.get("port") or 22)
        except (TypeError, ValueError):
            abort(400, "포트는 숫자입니다.")
        if not 1 <= port <= 65535:
            abort(400, "포트는 1 에서 65535 사이입니다.")
        out["port"] = port
    if not for_update or "username" in data:
        if not username:
            abort(400, "계정을 입력해 주세요.")
        if not re.match(r"^[A-Za-z0-9._-]{1,64}$", username):
            abort(400, "계정에 쓸 수 없는 문자가 있습니다.")
        out["username"] = username

    secret = None
    if not for_update or "auth_kind" in data:
        kind = (data.get("auth_kind") or store.AUTH_KEY).strip().lower()
        if kind not in store.AUTH_KINDS:
            abort(400, "인증 방식이 올바르지 않습니다.")
        out["auth_kind"] = kind
    if "key_name" in data or not for_update:
        key_name = (data.get("key_name") or "").strip()
        if key_name and not re.match(r"^[A-Za-z0-9._-]{1,80}$", key_name):
            abort(400, "키 이름에 쓸 수 없는 문자가 있습니다.")
        out["key_name"] = key_name
    if "description" in data or not for_update:
        out["description"] = (data.get("description") or "").strip()[:300]
    if "is_enabled" in data:
        out["is_enabled"] = 1 if data.get("is_enabled") else 0

    if "password" in data:
        raw = data.get("password") or ""
        if raw:
            if len(raw) > 200:
                abort(400, "비밀번호가 너무 깁니다.")
            secret = raw
    if out.get("auth_kind") == store.AUTH_PASSWORD and not for_update and not secret:
        abort(400, "비밀번호 인증을 고르면 비밀번호를 입력해야 합니다.")
    if out.get("auth_kind") == store.AUTH_KEY and not out.get("key_name") and not for_update:
        abort(400, "키 인증을 고르면 중계 PC 에 있는 키 이름을 적어야 합니다.")
    return out, secret


def _secret_for_db(secret):
    """
    비밀번호를 암호화해서 돌려준다. 암호화할 수 없으면 **저장하지 않는다.**

    서버 추가 화면에는 "접속 정보는 암호화해서 저장한다" 고 적혀 있다.
    cryptography 가 없으면 그 약속을 못 지키는데, 조용히 평문으로 넣으면
    적어 둔 말이 거짓이 된다. 이건 다른 서버로 들어가는 열쇠라서, DB 파일
    하나가 새면 그 서버까지 같이 넘어간다. 설정의 API Key 와 달리 막아도
    길이 막히지 않는다. 키 인증은 열쇠가 중계 PC 에 있고 DB 에는 이름만
    남으므로 이 제약과 상관없이 쓸 수 있다.

    requirements.txt 에 cryptography 가 들어 있으니 제대로 설치한 서버는
    여기에 걸리지 않는다. 걸린다면 설치가 덜 된 것이고, 그때는 조용히
    넘기지 않고 알려 주는 쪽이 맞다.
    """
    if not secret:
        return ""
    if not settings_store.encryption_available():
        abort(409, "비밀번호를 암호화할 수 없어 저장하지 않았습니다. 서버에서 "
                   "pip install cryptography 로 설치한 뒤 다시 저장하거나, "
                   "키 인증으로 등록해 주세요.")
    return settings_store.encrypt_secret(secret)


@api.post("/servers")
@auth.menu_required("servers")
def create_server():
    """
    서버를 더하는 것은 관리자만 한다.

    접속 정보를 넣는 일이고, 넣고 나면 그 서버에 붙을 수 있는 사람이 생긴다.
    목록을 볼 수 있는 사람 전부가 할 일이 아니다.
    """
    db, user = get_db(), _me()
    permissions.require_admin(user)
    data = request.get_json(silent=True) or {}
    fields, secret = _read_server_form(data)

    if store.get_server_by_name(db, fields["name"]):
        abort(409, "같은 이름의 서버가 이미 있습니다.")

    now = ts()
    cur = db.execute(
        "INSERT INTO ssh_servers (name, host, port, username, auth_kind, key_name,"
        " secret_enc, description, is_enabled, created_at, updated_at, created_by)"
        " VALUES (?,?,?,?,?,?,?,?,1,?,?,?)",
        (fields["name"], fields["host"], fields["port"], fields["username"],
         fields["auth_kind"], fields.get("key_name", ""),
         _secret_for_db(secret),
         fields.get("description", ""), now, now, user["id"]))
    sid = cur.lastrowid
    # 값은 적지 않는다. 무엇을 바꿨는지만 적는다.
    audit(db, user["id"], "ssh_server_added", "ssh_server", sid,
          "name=%s auth=%s" % (fields["name"], fields["auth_kind"]))
    db.commit()
    return jsonify(ok=True, server=store.server_payload(store.get_server(db, sid),
                                                        with_admin=True)), 201


@api.patch("/servers/<int:server_id>")
@auth.menu_required("servers")
def update_server(server_id):
    db, user = get_db(), _me()
    permissions.require_admin(user)
    row = store.get_server(db, server_id)
    if row is None:
        abort(404, "서버를 찾을 수 없습니다.")
    data = request.get_json(silent=True) or {}
    fields, secret = _read_server_form(data, for_update=True)

    if "name" in fields and fields["name"] != row["name"]:
        other = store.get_server_by_name(db, fields["name"])
        if other is not None and other["id"] != server_id:
            abort(409, "같은 이름의 서버가 이미 있습니다.")

    sets, vals = [], []
    for k, v in fields.items():
        sets.append("%s = ?" % k)
        vals.append(v)
    if secret:
        sets.append("secret_enc = ?")
        vals.append(_secret_for_db(secret))
    # 키 인증으로 바꾸면 저장해 둔 비밀번호를 지운다. 쓰지 않는 비밀을
    # 들고 있을 이유가 없다.
    if fields.get("auth_kind") == store.AUTH_KEY:
        sets.append("secret_enc = ''")
    if not sets:
        abort(400, "변경할 내용이 없습니다.")
    sets.append("updated_at = ?")
    vals += [ts(), server_id]
    db.execute("UPDATE ssh_servers SET %s WHERE id = ?" % ", ".join(sets), vals)
    audit(db, user["id"], "ssh_server_changed", "ssh_server", server_id,
          "fields=%s%s" % (",".join(sorted(fields)), "+secret" if secret else ""))
    db.commit()
    return jsonify(ok=True, server=store.server_payload(store.get_server(db, server_id),
                                                        with_admin=True))


@api.delete("/servers/<int:server_id>")
@auth.menu_required("servers")
def delete_server(server_id):
    """
    서버를 목록에서 뺀다. 열려 있는 터미널은 먼저 닫는다.

    대화에 붙어 있던 서버가 사라지면 그 대화는 '서버 없음' 이 된다(ON DELETE
    SET NULL). 대화와 기록은 지우지 않는다.
    """
    db, user = get_db(), _me()
    permissions.require_admin(user)
    row = store.get_server(db, server_id)
    if row is None:
        abort(404, "서버를 찾을 수 없습니다.")

    terms = db.execute(
        "SELECT id, user_id FROM term_sessions WHERE server_id = ?"
        "   AND state IN ('opening','open')", (server_id,)).fetchall()
    for t in terms:
        stream = store.HUB.get(t["id"])
        if stream:
            stream.close("서버가 목록에서 제거되었습니다")
        store.set_term_state(db, t["id"], "closed", "서버 삭제")
        store.enqueue(db, store.KIND_TERM_CLOSE, t["user_id"], term_id=t["id"])

    tunnels = tstore.close_server_tunnels(db, server_id, "서버가 목록에서 빠져 닫았습니다")

    # 붙어 있던 대화를 먼저 떼어 낸다.
    #
    # sessions.server_id 는 ALTER TABLE 로 더한 컬럼이라 ON DELETE 규칙이 없다.
    # (sqlite 는 나중에 제약만 바꿀 수 없다) 그래서 코드에서 명시적으로 끊는다.
    # 이렇게 해 두면 새로 만든 DB 와 옮겨 온 DB 가 똑같이 동작한다.
    detached = db.execute(
        "UPDATE sessions SET server_id = NULL WHERE server_id = ?",
        (server_id,)).rowcount

    db.execute("DELETE FROM ssh_servers WHERE id = ?", (server_id,))
    audit(db, user["id"], "ssh_server_removed", "ssh_server", server_id,
          "name=%s terms_closed=%d tunnels_closed=%d sessions_detached=%d"
          % (row["name"], len(terms), tunnels, detached))
    db.commit()
    store.wake()
    return jsonify(ok=True, deleted=server_id, terminals_closed=len(terms),
                   sessions_detached=detached)


# ---------------------------------------------------------------------------
# 내 중계
#
# 사람마다 자기 VDI 에 깐다. 그래서 등록 코드도 프로그램도 **쓰는 사람이**
# 직접 받는다. 관리자를 거치면 사람이 늘 때마다 관리자가 병목이 된다.
# ---------------------------------------------------------------------------
@api.get("/my-relay")
@auth.menu_required("servers")
def my_relay():
    db, user = get_db(), _me()
    return jsonify(ok=True, relay=_my_relay_payload(db, user))


@api.post("/my-relay/enroll")
@auth.menu_required("servers")
def my_enroll_code():
    """
    내 등록 코드를 받는다. 원문은 **이 응답에만** 있다.

    내가 받은 코드로 등록한 중계는 내 중계가 된다. 새로 받으면 내가 전에 받은
    코드만 죽는다. 남의 코드는 건드리지 않는다.
    """
    db, user = get_db(), _me()
    code = store.new_enroll_code(db, user["id"])
    audit(db, user["id"], "relay_enroll_code_issued", "relay", "", "self ttl=600s")
    db.commit()
    return jsonify(ok=True, code=code, expires_in=config.RELAY_ENROLL_TTL_SECONDS,
                   server_url=request.url_root.rstrip("/"))


@api.post("/my-relay/revoke")
@auth.menu_required("servers")
def my_relay_revoke():
    """내 중계를 끊는다. (VDI 를 바꿀 때 쓴다) 남의 중계는 건드리지 못한다."""
    db, user = get_db(), _me()
    agent = store.live_agent(db, user["id"])
    if agent is None:
        abort(404, "붙어 있는 내 중계가 없습니다.")
    store.revoke_agent(db, agent["id"])
    closed = store.close_user_terms(db, user["id"], "중계 등록을 해제했습니다")
    tstore.close_user_tunnels(db, user["id"], "VDI 중계 등록을 해제해 터널을 닫았습니다")
    audit(db, user["id"], "relay_revoked", "relay", agent["id"], "self")
    db.commit()
    store.wake()
    return jsonify(ok=True, terminals_closed=closed)


@bp.get("/servers/program")
@auth.menu_required("servers")
def download_program():
    """
    중계 프로그램을 받는다.

    관리자가 올려 둔 파일 하나를 그대로 내려 준다. 경로를 요청에서 받지 않는다.
    (받은 경로로 파일을 찾아 주면 그 한 줄이 파일시스템 전체를 연다)
    """
    db, user = get_db(), _me()
    meta = store.program_info()
    if meta is None:
        abort(404, "아직 중계 프로그램이 올라와 있지 않습니다. 관리자에게 "
                   "요청하세요.")
    audit(db, user["id"], "relay_program_downloaded", "relay", "",
          "name=%s sha256=%s" % (meta["name"], meta["sha256"][:12]))
    db.commit()
    return send_file(meta["path"], as_attachment=True,
                     download_name=meta["name"],
                     mimetype="application/octet-stream")


@api.get("/servers/<int:server_id>/sessions")
@auth.menu_required("servers")
def server_sessions(server_id):
    """
    「채팅」을 눌렀을 때 보여 줄 "이어서 쓸 대화" 목록.

        same   : 이 서버에 이미 붙어 있는 내 대화
        others : 서버가 다르거나 없는 최근 내 대화 (고르면 서버가 바뀐다)

    남의 대화는 나오지 않는다. 공개 대화도 나오지 않는다. 서버를 붙인 대화는
    공개로 둘 수 없으므로 공개 대화를 여기서 고르게 하면 그 자리에서 거절당한다.
    """
    db, user = get_db(), _me()
    permissions.require_menu(db, user, "chat")
    _server_or_403(db, server_id)

    rows = db.execute(
        "SELECT s.*, v.name AS srv_name FROM sessions s"
        " LEFT JOIN ssh_servers v ON v.id = s.server_id"
        " WHERE s.owner_id = ? AND s.visibility = 'private'"
        " ORDER BY s.updated_at DESC LIMIT 40", (user["id"],)).fetchall()

    def item(r):
        return {"id": r["id"], "name": r["name"], "updated_at": r["updated_at"],
                "server": ({"id": r["server_id"], "name": r["srv_name"]}
                           if r["server_id"] and r["srv_name"] else None)}

    same = [item(r) for r in rows if r["server_id"] == server_id][:5]
    others = [item(r) for r in rows if r["server_id"] != server_id][:5]
    return jsonify(ok=True, same=same, others=others)


# ---------------------------------------------------------------------------
# 연결 테스트
#
# 저장 전(초안)과 저장 후(등록된 서버) 양쪽에서 쓴다. 저장 전 테스트가
# 성공해야 저장 단추가 눌린다. 주소 하나 틀린 서버가 목록에 남아 있으면
# 나중에 "왜 안 되지" 를 사람마다 한 번씩 겪는다.
# ---------------------------------------------------------------------------
def _run_test(db, user, server_id=None, draft=None):
    _require_my_relay(db, user)
    payload = {}
    if draft is not None:
        # 큐에는 비밀이 아닌 것만 적는다. 어디에 붙어 보려 했는지는 기록으로
        # 남을 값이고, 비밀번호는 남아서는 안 되는 값이다.
        payload["draft_host"] = "%s@%s:%s" % (draft.get("username"),
                                              draft.get("host"), draft.get("port"))
    job_id = store.enqueue(db, store.KIND_TEST, user["id"], server_id=server_id,
                           payload=payload)
    if draft is not None:
        store.put_side("draft", job_id, draft)
    db.commit()
    store.wake()

    timeout = min(30, store.run_timeout(db))
    row = store.wait_job(db, job_id, timeout)
    if row is None:
        abort(500, "테스트 요청이 사라졌습니다. 다시 시도해 주세요.")
    if row["state"] in ("queued", "taken"):
        store.cancel_job(db, job_id, "시간 초과")
        db.commit()
        return False, "중계가 %d초 안에 답하지 않았습니다." % timeout, {}
    try:
        res = json.loads(row["result"] or "{}")
    except ValueError:
        res = {}
    ok = bool(row["ok"])
    return ok, res.get("message") or ("연결 확인" if ok else "연결 실패"), res


@api.post("/servers/test")
@auth.menu_required("servers")
def test_draft():
    """저장하기 전의 초안으로 테스트한다. 아직 DB 에 없는 서버다."""
    db, user = get_db(), _me()
    permissions.require_admin(user)
    data = request.get_json(silent=True) or {}
    fields, secret = _read_server_form(data)

    draft = {"host": fields["host"], "port": fields["port"],
             "username": fields["username"], "kind": fields["auth_kind"],
             "key_name": fields.get("key_name", "")}
    if fields["auth_kind"] == store.AUTH_PASSWORD:
        if not secret:
            abort(400, "비밀번호를 입력해 주세요.")
        draft["password"] = secret

    ok, message, res = _run_test(db, user, server_id=None, draft=draft)
    audit(db, user["id"], "ssh_server_test", "ssh_server", "",
          "draft host=%s ok=%s" % (fields["host"], ok))
    db.commit()
    return jsonify(ok=ok, message=message, banner=res.get("banner", ""),
                   elapsed=res.get("elapsed"))


@api.post("/servers/<int:server_id>/test")
@auth.menu_required("servers")
def test_server(server_id):
    """등록된 서버를 테스트한다. 그 서버를 쓸 수 있는 사람이면 누를 수 있다."""
    db, user = get_db(), _me()
    row = _server_or_403(db, server_id)
    if not row["is_enabled"]:
        abort(409, "꺼 둔 서버입니다.")
    ok, message, res = _run_test(db, user, server_id=server_id)
    store.touch_server_check(db, server_id, ok, message)
    audit(db, user["id"], "ssh_server_test", "ssh_server", server_id,
          "name=%s ok=%s" % (row["name"], ok))
    db.commit()
    return jsonify(ok=ok, message=message, banner=res.get("banner", ""),
                   elapsed=res.get("elapsed"),
                   server=store.server_payload(store.get_server(db, server_id)))


# ---------------------------------------------------------------------------
# 웹 터미널
# ---------------------------------------------------------------------------
def _term_or_403(db, term_id):
    """
    내 터미널만 열 수 있다. 관리자도 남의 터미널 화면은 보지 못한다.

    private 대화를 관리자가 못 보는 것과 같은 이유다. 운영 권한은 "누가 언제
    무엇을 했는지" 를 보는 것이고, 그 사람의 화면에 함께 앉는 것이 아니다.
    """
    row = store.term_row(db, term_id)
    if row is None:
        abort(404, "터미널을 찾을 수 없습니다.")
    if row["user_id"] != _me()["id"]:
        abort(403, "내가 연 터미널만 쓸 수 있습니다.")
    return row


@api.get("/term")
@auth.menu_required("servers")
def my_terms():
    db, user = get_db(), _me()
    rows = store.open_terms_for_user(db, user["id"])
    return jsonify(ok=True,
                   terminals=[{"term_id": r["id"], "server_id": r["server_id"],
                               "server_name": r["server_name"], "state": r["state"],
                               "opened_at": r["opened_at"]} for r in rows],
                   max=store.term_max_per_user(db))


@api.post("/term")
@auth.menu_required("servers")
def open_term():
    db, user = get_db(), _me()
    data = request.get_json(silent=True) or {}
    try:
        server_id = int(data.get("server_id") or 0)
    except (TypeError, ValueError):
        abort(400, "서버를 고르세요.")
    row = _server_or_403(db, server_id)
    permissions.require_shell(db, user, permissions.SHELL_CONSOLE)
    if not row["is_enabled"]:
        abort(409, "꺼 둔 서버입니다. 관리자에게 문의하세요.")
    _require_my_relay(db, user)

    # 웹 콘솔만 센다. 클라이언트의 PuTTY 탭은 relay_tunnel_max_per_user 가 따로 센다.
    limit = store.term_max_per_user(db)
    if store.open_term_count(db, user["id"]) >= limit:
        abort(409, "한 사람이 동시에 열 수 있는 웹 콘솔은 %d개까지입니다. 쓰지 않는 "
                   "것을 먼저 닫아 주세요." % limit)

    cols = max(40, min(200, int(data.get("cols") or 120)))
    rows_n = max(10, min(60, int(data.get("rows") or 30)))

    term_id = uuid.uuid4().hex
    store.open_term_row(db, term_id, server_id, user["id"])
    store.HUB.create(term_id, server_id, user["id"])
    store.enqueue(db, store.KIND_TERM_OPEN, user["id"], server_id=server_id,
                  term_id=term_id, payload={"cols": cols, "rows": rows_n})
    audit(db, user["id"], "term_opened", "ssh_server", server_id,
          "name=%s term=%s" % (row["name"], term_id[:8]))
    db.commit()
    store.wake()
    return jsonify(ok=True, term_id=term_id, poll_ms=250,
                   max_input=config.RELAY_TERM_MAX_INPUT_BYTES), 201


@api.get("/term/<term_id>/io")
@auth.menu_required("servers")
def term_read(term_id):
    """
    화면을 받아 간다. 기다리지 않고 바로 돌려준다.

    스레드가 8개뿐이라 여기서 응답을 붙잡으면 채팅이 밀린다. 그래서 0.25초
    간격의 짧은 폴링이고, 서버는 있는 것만 주고 바로 끝낸다.
    """
    db = get_db()
    row = _term_or_403(db, term_id)
    try:
        after = int(request.args.get("seq") or 0)
    except (TypeError, ValueError):
        after = 0

    stream = store.HUB.get(term_id)
    if stream is None:
        # 서버가 재시작되면 메모리 버퍼가 사라진다. 화면은 닫힌 것으로 본다.
        return jsonify(ok=True, seq=after, data="", state="closed",
                       reason=row["close_reason"] or
                              "서버가 재시작되어 이 터미널은 닫혔습니다",
                       dropped=False, silence=None)
    out = stream.read_after(after)
    out["ok"] = True
    if out["state"] == "closed" and row["state"] not in ("closed", "failed"):
        store.set_term_state(db, term_id, "closed", out["reason"])
        db.commit()
    return jsonify(**out)


@api.post("/term/<term_id>/io")
@auth.menu_required("servers")
def term_write(term_id):
    db = get_db()
    row = _term_or_403(db, term_id)
    # 셸 허용은 여는 순간만이 아니라 **칠 때마다** 본다. 허용을 뗀 뒤 청소가
    # 돌기 전의 몇 초 사이에도 키가 나가면 안 된다.
    permissions.require_shell(db, _me(), permissions.SHELL_CONSOLE)
    if row["state"] not in ("opening", "open"):
        abort(409, "이미 닫힌 터미널입니다.")
    data = request.get_json(silent=True) or {}
    text = data.get("data")
    if not isinstance(text, str) or text == "":
        abort(400, "보낼 내용이 없습니다.")
    if len(text.encode("utf-8", "replace")) > config.RELAY_TERM_MAX_INPUT_BYTES:
        abort(413, "한 번에 보낼 수 있는 양을 넘었습니다. (최대 %dKB) "
                   "붙여넣기는 나눠서 보내 주세요."
              % (config.RELAY_TERM_MAX_INPUT_BYTES // 1024))

    stream = store.HUB.get(term_id)
    if stream is None or stream.state == "closed":
        abort(409, "닫힌 터미널입니다. 다시 열어 주세요.")

    # 기록은 엔터로 끝난 줄만 남긴다. 비밀번호 프롬프트 뒤의 줄은 가린다.
    lines = stream.feed_record(text)
    stream.send(text)
    if lines:
        store.record_term_lines(db, term_id, lines)
        db.commit()
    return jsonify(ok=True, recorded=len(lines))


@api.post("/term/<term_id>/close")
@auth.menu_required("servers")
def term_close(term_id):
    db, user = get_db(), _me()
    row = _term_or_403(db, term_id)
    stream = store.HUB.get(term_id)
    if stream:
        stream.close("사용자가 연결을 끊었습니다")
    if row["state"] in ("opening", "open"):
        store.set_term_state(db, term_id, "closed", "사용자가 닫음")
        store.enqueue(db, store.KIND_TERM_CLOSE, user["id"], term_id=term_id)
        audit(db, user["id"], "term_closed", "ssh_server", row["server_id"],
              "term=%s lines=%d" % (term_id[:8], row["lines_in"]))
    db.commit()
    store.wake()
    return jsonify(ok=True, closed=term_id)


# ---------------------------------------------------------------------------
# 승인 카드
# ---------------------------------------------------------------------------
def _command_or_404(db, cmd_id):
    row = store.get_command(db, cmd_id)
    if row is None:
        abort(404, "명령을 찾을 수 없습니다.")
    return row


def _can_approve(db, user, cmd_row):
    """
    승인은 **그 대화를 쓰는 사람**이 한다.

    자기가 시킨 일을 자기가 승인하는 것이 이상하게 보일 수 있다. 그렇지 않다.
    승인 카드는 "다른 사람의 결재" 가 아니라 "챗봇이 고른 명령을 사람이 한 번
    더 본다" 는 장치다. 사람 둘을 세우면 둘 다 안 보고 누르게 된다.
    """
    sess = db.execute("SELECT * FROM sessions WHERE id = ?",
                      (cmd_row["session_id"],)).fetchone()
    if sess is None:
        return False, None
    if not permissions.can_write_session(db, user, sess):
        return False, sess
    return True, sess


@api.post("/ssh/commands/<int:cmd_id>/approve")
@auth.menu_required("chat")
def approve_command(cmd_id):
    """
    승인하고 실행한다. 결과는 그 대화에 메시지로 남고, 기록에는 "누가 언제
    승인했는지" 가 남는다.
    """
    db, user = get_db(), _me()
    store.expire_pending(db)
    db.commit()
    row = _command_or_404(db, cmd_id)
    ok_who, sess = _can_approve(db, user, row)
    if not ok_who:
        abort(403, "이 대화에서 승인할 권한이 없습니다.")
    if row["state"] != "pending":
        abort(409, _state_message(row["state"]))
    if row["level"] == ssh_policy.BLOCKED:
        abort(403, "실행하지 않는 명령입니다.")
    if not permissions.ssh_can_write(db, user):
        abort(403, "변경 명령을 실행할 권한이 없습니다. 지금 정책은 '%s' 입니다."
              % permissions.SSH_LEVEL_LABELS[permissions.ssh_level(db, user)])

    server = store.get_server(db, row["server_id"])
    permissions.require_server(db, user, server)
    if not server["is_enabled"]:
        abort(409, "꺼 둔 서버입니다.")
    _require_my_relay(db, user)

    db.execute("UPDATE ssh_commands SET state = 'running', approved_by = ?,"
               " approved_at = ? WHERE id = ? AND state = 'pending'",
               (user["id"], ts(), cmd_id))
    db.commit()

    ran = _execute_command(db, user, row)
    audit(db, user["id"], "ssh_command_approved", "ssh_server", row["server_id"],
          "cmd=%d level=%s ok=%s" % (cmd_id, row["level"], ran["ok"]))
    db.commit()

    msg_id = _append_result_message(db, row, ran)
    return jsonify(ok=True, command=store.command_payload(db, store.get_command(db, cmd_id)),
                   message_id=msg_id, result={"ok": ran["ok"], "elapsed": ran["elapsed"],
                                              "exit_code": ran["exit_code"],
                                              "error": ran["error"]})


@api.post("/ssh/commands/<int:cmd_id>/reject")
@auth.menu_required("chat")
def reject_command(cmd_id):
    db, user = get_db(), _me()
    row = _command_or_404(db, cmd_id)
    ok_who, _sess = _can_approve(db, user, row)
    if not ok_who:
        abort(403, "이 대화에서 거절할 권한이 없습니다.")
    if row["state"] != "pending":
        abort(409, _state_message(row["state"]))
    db.execute("UPDATE ssh_commands SET state = 'rejected', finished_at = ?,"
               " approved_by = ?, approved_at = ?, reason = '사람이 거절했습니다'"
               " WHERE id = ? AND state = 'pending'",
               (ts(), user["id"], ts(), cmd_id))
    audit(db, user["id"], "ssh_command_rejected", "ssh_server", row["server_id"],
          "cmd=%d" % cmd_id)
    db.commit()
    return jsonify(ok=True, command=store.command_payload(db, store.get_command(db, cmd_id)))


def _state_message(state):
    return {
        "approved": "이미 승인된 명령입니다.",
        "running": "이미 실행 중입니다.",
        "done": "이미 실행이 끝난 명령입니다.",
        "failed": "이미 실행했고 실패한 명령입니다.",
        "rejected": "이미 거절한 명령입니다.",
        "expired": "승인 시간이 지나 취소되었습니다. 다시 물어보세요.",
        "blocked": "실행하지 않는 명령입니다.",
        "denied": "권한이 없어 실행되지 않은 명령입니다.",
    }.get(state, "지금 상태에서는 할 수 없습니다.")


# ---------------------------------------------------------------------------
# 명령 실행 (챗봇이 고른 것)
# ---------------------------------------------------------------------------
def _execute_command(db, user, cmd_row):
    """
    중계에 명령 하나를 보내고 결과를 받는다.

    반환: {ok, exit_code, output, elapsed, error}
    output 은 **대화에만** 들어간다. 기록(ssh_commands)에는 요약만 적는다.
    """
    timeout = store.run_timeout(db)
    job_id = store.enqueue(db, store.KIND_RUN, user["id"],
                           server_id=cmd_row["server_id"],
                           payload={"command": cmd_row["command"],
                                    "timeout": timeout})
    db.execute("UPDATE ssh_commands SET job_id = ? WHERE id = ?", (job_id, cmd_row["id"]))
    db.commit()
    store.wake()

    started = time.time()
    job = store.wait_job(db, job_id, timeout + 5)
    elapsed = round(time.time() - started, 2)

    out = {"ok": False, "exit_code": None, "output": "", "elapsed": elapsed, "error": ""}
    if job is None:
        out["error"] = "실행 요청이 사라졌습니다."
    elif job["state"] in ("queued", "taken"):
        store.cancel_job(db, job_id, "시간 초과")
        out["error"] = "중계가 %d초 안에 끝내지 못했습니다." % timeout
    else:
        try:
            res = json.loads(job["result"] or "{}")
        except ValueError:
            res = {}
        out["ok"] = bool(job["ok"])
        out["exit_code"] = res.get("exit_code")
        # 출력은 DB 가 아니라 메모리에서 받는다. (relay_result 가 넣어 둔다)
        out["output"] = store.take_side("output", job_id) or ""
        out["error"] = res.get("error") or ""
        if res.get("elapsed"):
            out["elapsed"] = res["elapsed"]

    db.execute(
        "UPDATE ssh_commands SET state = ?, finished_at = ?, exit_code = ?,"
        " duration_ms = ?, result_note = ?, reason = CASE WHEN ? = '' THEN reason ELSE ? END"
        " WHERE id = ?",
        ("done" if out["ok"] else "failed", ts(), out["exit_code"],
         int(out["elapsed"] * 1000), ssh_policy.result_note(out["exit_code"], out["output"]),
         out["error"], out["error"], cmd_row["id"]))
    db.commit()
    return out


def _fence(text, limit=4000):
    """결과를 대화에 넣을 모양. 길면 잘라내고 잘랐다고 적는다."""
    body = (text or "").replace("\r\n", "\n").rstrip()
    if not body:
        return "(출력 없음)"
    if len(body) > limit:
        body = body[:limit] + "\n… (%d자 더 있음. 전체는 터미널에서 보세요)" % (
            len(text) - limit)
    return body


def _append_result_message(db, cmd_row, ran):
    """
    승인 뒤의 결과를 대화에 남긴다.

    여기서 Claude 를 다시 부르지 않는다. 승인해서 실행한 명령의 결과는
    요약하지 않고 그대로 보여 주는 것이 맞다. 요약하는 과정에서 숫자 하나가
    달라지면 승인한 사람이 확인할 수 없게 된다.
    """
    head = "승인 후 실행" if ran["ok"] else "승인 후 실행 실패"
    lines = ["**%s**" % head, "", "```", "$ " + cmd_row["command"], "",
             _fence(ran["output"]), "```"]
    tail = "종료 %s · %s초" % ("?" if ran["exit_code"] is None else ran["exit_code"],
                              ran["elapsed"])
    if ran["error"]:
        tail += " · " + ran["error"]
    lines += ["", tail]
    content = "\n".join(lines)

    cur = db.execute(
        "INSERT INTO messages (session_id, user_id, role, content, created_at)"
        " VALUES (?,NULL,?,?,?)",
        (cmd_row["session_id"], "assistant" if ran["ok"] else "error", content, ts()))
    msg_id = cur.lastrowid
    # 결과 메시지에 그 명령 카드를 붙여 둔다. 승인한 사람과 시각이 결과와
    # 같은 자리에 보여야 한다. 결과만 남기면 누가 승인했는지 사라진다.
    db.execute("UPDATE ssh_commands SET message_id = ? WHERE id = ?",
               (msg_id, cmd_row["id"]))
    db.execute("UPDATE sessions SET updated_at = ? WHERE id = ?",
               (ts(), cmd_row["session_id"]))
    db.commit()
    return msg_id


# ---------------------------------------------------------------------------
# 채팅에서 부르는 자리
#
# app.py 의 post_message 가 Claude 의 답을 받은 뒤 이 함수를 부른다. 챗봇이
# ```ssh 블록으로 명령을 고르면
#
#     조회 : 바로 실행하고 결과를 Claude 에 다시 넘겨 답을 받는다
#     변경 : 승인 카드를 만들고 멈춘다 (사람이 누르기 전에는 나가지 않는다)
#     차단 : 아무것도 하지 않고 왜 막혔는지 적는다
# ---------------------------------------------------------------------------
# 여러 줄 블록(```ssh 다음 줄에 명령)과 한 줄 블록(```ssh df -h```) 둘 다 잡는다.
# ```console 이나 언어 없는 블록은 잡지 않는다. 그런 블록에는 설명용 예시나
# 출력이 들어 있어서, 잡으면 명령이 아닌 것이 서버로 나간다.
_SSH_BLOCK = re.compile(
    r"```(?:ssh|bash|shell|sh)(?:[ \t]*\n(.*?)|[ \t]+([^\n`]+?))```", re.DOTALL)


def extract_commands(text):
    """답에서 ```ssh 블록을 꺼낸다. 반환: [명령문]"""
    out = []
    for m in _SSH_BLOCK.finditer(text or ""):
        body = (m.group(1) or m.group(2) or "").strip()
        if body:
            out.append(body)
    return out


def strip_blocks(text):
    """카드로 대신 보여 줄 블록을 본문에서 지운다."""
    cleaned = _SSH_BLOCK.sub("", text or "")
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return cleaned


def chat_prompt_prefix(db, server_row, level, max_cmds):
    """서버가 붙은 대화에서 질문 앞에 붙이는 안내."""
    if server_row is None:
        return ("[서버 안내]\n"
                "이 대화에는 붙은 서버가 없다. 서버 상태를 확인해야 하는 질문에는 "
                "확인할 수 없다고 답하고, 아래쪽 서버 칩에서 서버를 고르라고 알려 줘라.\n\n")

    what = ("조회 명령만 쓸 수 있다. 바꾸는 명령은 지금 이 사용자에게 허용되지 "
            "않았으므로 제안만 하고 블록으로 쓰지 마라."
            if level != permissions.SSH_WRITE else
            "바꾸는 명령도 쓸 수 있다. 바꾸는 명령은 사람이 승인 카드를 눌러야 "
            "실행되므로, 쓴 뒤에 결과를 기다리지 말고 무엇을 하려는지만 설명해라.")
    return (
        "[서버 작업 안내]\n"
        "이 대화에는 서버 %s (%s@%s) 가 붙어 있다. %s\n"
        "서버를 직접 봐야 답할 수 있으면 다음 형식으로 명령을 적어라.\n"
        "```ssh\n명령 한 줄\n```\n"
        "- 블록 하나에 명령 하나. 설명은 블록 밖에 쓴다.\n"
        "- 조회 명령은 이 응답 안에서 바로 실행되고, 그 결과와 함께 너에게 다시 "
        "묻는다. 그때 사람 말로 답해라. 숫자는 결과에 있는 값을 그대로 쓴다.\n"
        "- 블록 없이 \"확인하겠습니다\", \"결과가 오면 정리하겠습니다\" 로 끝내지 "
        "마라. 블록이 없으면 아무것도 실행되지 않고, 사용자는 오지 않을 답을 "
        "기다리게 된다.\n"
        "- 한 번에 최대 %d개까지. 추측으로 답하지 말고 모르면 명령을 적어라.\n"
        "- 비밀번호나 키가 들어 있는 파일은 읽지 않는다.\n\n"
        % (server_row["name"], server_row["username"], server_row["host"], what, max_cmds))


def handle_chat_reply(db, user, sess, reply, ask_again, deadline=None,
                      claude_timeout=180):
    """
    Claude 의 답에 든 명령을 처리한다.

    ask_again(prompt) : 결과를 넘겨 다시 물어보는 콜백. app.py 가 넘긴다.
                        (provider 를 relay 가 직접 들고 있지 않게 한다)

    반환: (최종 본문, [ssh_commands.id], 요약 dict)

    만든 명령 행은 아직 어느 메시지에도 붙어 있지 않다. 답 본문이 이 함수의
    결과로 정해지므로 메시지 행을 먼저 만들 수 없다. app.py 가 메시지를 넣은
    뒤에 attach_commands 로 붙인다.
    """
    message_id = None
    server = store.get_server(db, sess["server_id"]) if sess["server_id"] else None
    level = permissions.ssh_level(db, user)
    max_cmds = store.chat_max_commands(db)
    made, used, rounds = [], 0, 0
    text = reply or ""
    note = {"ran": 0, "pending": 0, "blocked": 0, "denied": 0, "cut": False}
    timeout = store.run_timeout(db)

    def time_left_for(seconds):
        """남은 시간 안에 그만큼 걸릴 일을 시작해도 되는가."""
        if deadline is None:
            return True
        return time.time() + seconds <= deadline

    while rounds < 3:
        cmds = extract_commands(text)
        if not cmds:
            break
        outputs, stop = [], False
        for raw in cmds:
            if used >= max_cmds:
                stop = True
                break
            used += 1
            if server is None:
                made.append(store.create_command(
                    db, None, sess["id"], message_id, user["id"], raw,
                    ssh_policy.BLOCKED, "blocked", "이 대화에는 붙은 서버가 없다"))
                note["blocked"] += 1
                stop = True
                continue

            verdict = ssh_policy.classify(raw)
            lv, why = verdict["level"], verdict["reason"]

            if lv == ssh_policy.BLOCKED:
                made.append(store.create_command(
                    db, server["id"], sess["id"], message_id, user["id"], raw,
                    lv, "blocked", why))
                note["blocked"] += 1
                stop = True
                continue

            if lv == ssh_policy.READ and not permissions.ssh_can_read(db, user):
                made.append(store.create_command(
                    db, server["id"], sess["id"], message_id, user["id"], raw,
                    lv, "denied", "서버 사용이 허용되지 않았습니다"))
                note["denied"] += 1
                stop = True
                continue

            if lv == ssh_policy.WRITE:
                if not permissions.ssh_can_write(db, user):
                    made.append(store.create_command(
                        db, server["id"], sess["id"], message_id, user["id"], raw,
                        lv, "denied",
                        "지금 정책은 '%s' 입니다. 변경 명령은 나가지 않습니다."
                        % permissions.SSH_LEVEL_LABELS[level]))
                    note["denied"] += 1
                else:
                    made.append(store.create_command(
                        db, server["id"], sess["id"], message_id, user["id"], raw,
                        lv, "pending", why))
                    note["pending"] += 1
                stop = True        # 승인을 기다리는 동안 더 진행하지 않는다
                continue

            # 조회 : 승인 없이 실행한다
            if not time_left_for(timeout):
                note["cut"] = True
                stop = True
                used -= 1
                break
            cid = store.create_command(db, server["id"], sess["id"], message_id,
                                       user["id"], raw, lv, "running")
            db.commit()
            ran = _execute_command(db, user, store.get_command(db, cid))
            made.append(cid)
            note["ran"] += 1
            outputs.append((raw, ran))

        if stop or not outputs:
            break

        # 결과를 넘겨 다시 묻는다
        parts = ["[명령 실행 결과]"]
        for raw, ran in outputs:
            parts.append("$ %s" % raw)
            parts.append(_fence(ran["output"], 6000))
            parts.append("(종료 %s · %s초)" % (
                "?" if ran["exit_code"] is None else ran["exit_code"], ran["elapsed"]))
            if ran["error"]:
                parts.append("(오류: %s)" % ran["error"])
        parts.append("")
        parts.append("위 결과를 바탕으로 사람 말로 답해라. 결과에 없는 숫자를 "
                     "만들지 마라. 더 봐야 하면 명령을 한 번 더 적어도 된다.")

        # 다음 왕복을 시작하면 요청 전체가 gunicorn timeout 을 넘길 수 있는지
        # 먼저 본다. 넘길 것 같으면 지금 가진 결과만 그대로 보여 준다.
        # 워커가 죽으면 답도 사라지고 사용자는 아무 설명도 못 받는다.
        if not time_left_for(claude_timeout):
            note["cut"] = True
            text = _raw_result_text(strip_blocks(text), outputs,
                                    "시간이 길어져 결과를 그대로 보여 드립니다.")
            break

        # Claude 를 부르기 전에 트랜잭션을 닫는다. 이 호출은 최대 claude_timeout
        # (기본 180초)까지 걸린다. 쓰기 락을 들고 들어가면 그 시간 동안 서버
        # 전체의 쓰기가 멈춘다.
        db.commit()
        ok2, text2 = ask_again("\n".join(parts))
        rounds += 1
        if not ok2:
            text = _raw_result_text(strip_blocks(text), outputs,
                                    "명령은 실행했지만 결과를 정리하는 중에 "
                                    "실패했습니다.")
            break
        text = text2

    # 서버가 붙은 대화인데 답에 명령이 하나도 없었다. 화면이 그 사실을 적는다.
    note["none"] = used == 0
    final = strip_blocks(text)
    if not final:
        final = _no_text_fallback(note)
    return final, made, note


def _raw_result_text(head, outputs, why):
    """
    Claude 가 정리해 주지 못했을 때의 답.

    "실패했습니다" 한 줄만 돌려주면 명령은 이미 서버에서 돌았는데 사람은
    결과를 못 본다. 그래서 받은 것을 그대로 보여 준다.
    """
    lines = [head] if head else []
    lines += ["", why, ""]
    for raw, ran in outputs:
        lines += ["```", "$ " + raw, _fence(ran["output"]), "```"]
    return "\n".join(lines).strip()


def attach_commands(db, message_id, cmd_ids):
    """만든 명령 카드를 답 메시지에 붙인다."""
    if not cmd_ids:
        return
    db.execute("UPDATE ssh_commands SET message_id = ? WHERE id IN (%s)"
               % ",".join("?" for _ in cmd_ids), [message_id] + list(cmd_ids))


def _no_text_fallback(note):
    if note["pending"]:
        return "실행하려면 승인이 필요한 명령입니다. 아래 카드에서 확인해 주세요."
    if note["blocked"]:
        return "이 명령은 실행하지 않습니다. 아래에 이유를 적었습니다."
    if note["denied"]:
        return "지금 권한으로는 실행할 수 없는 명령입니다."
    return "명령을 실행했지만 돌려줄 설명이 없습니다. 다시 물어봐 주세요."


# ---------------------------------------------------------------------------
# 중계 프로그램용 API
#
# 쿠키를 쓰지 않는다. X-Relay-Key 헤더로만 인증한다. 브라우저의 form 이
# 이 헤더를 붙일 수 없으므로 CSRF 대상이 아니다.
# ---------------------------------------------------------------------------
_enroll_fail = {"count": 0, "until": 0.0}
_enroll_guard = threading.Lock()


def _enroll_blocked():
    with _enroll_guard:
        if _enroll_fail["until"] > time.time():
            return True
        if _enroll_fail["until"] and _enroll_fail["until"] <= time.time():
            _enroll_fail.update({"count": 0, "until": 0.0})
        return False


def _enroll_failed():
    """
    등록 코드는 6자리다. 짧은 대신 수명이 10분이고 한 번 쓰면 죽는다.
    그래도 찍어 보는 것을 막아야 해서 실패를 센다.
    """
    with _enroll_guard:
        _enroll_fail["count"] += 1
        if _enroll_fail["count"] >= config.RELAY_ENROLL_MAX_FAILURES:
            _enroll_fail["until"] = time.time() + 600
            _enroll_fail["count"] = 0


@relay_api.post("/register")
def relay_register():
    """
    등록. VDI 에서 사람이 적는 줄은 서버 주소와 이 코드 두 개뿐이다.
    성공하면 중계 토큰을 돌려준다. 토큰은 이 응답에만 있다.
    """
    db = get_db()
    if _enroll_blocked():
        abort(429, "등록 시도가 너무 많습니다. 10분 뒤에 다시 시도하세요.")
    data = request.get_json(silent=True) or {}
    code = str(data.get("code") or "")
    code_hash = store.consume_enroll_code(db, code)
    if code_hash is None:
        db.commit()
        _enroll_failed()
        audit(db, None, "relay_register_failed", "relay", "", "bad or expired code")
        db.commit()
        abort(403, "등록 코드가 틀렸거나 시간이 지났습니다. 관리자 화면에서 "
                   "새 코드를 받으세요.")

    agent_id, key = store.register_agent(
        db, code_hash,
        name=data.get("name") or "", version=data.get("version") or "",
        os_info=data.get("os") or "", ip=auth.client_ip(), scheme=_client_scheme())
    audit(db, None, "relay_registered", "relay", agent_id,
          "name=%s version=%s" % (data.get("name") or "", data.get("version") or ""))
    db.commit()
    return jsonify(ok=True, agent_key=key, agent_id=agent_id,
                   poll_seconds=store.poll_seconds(db)), 201


def _agent_or_401(db):
    key = request.headers.get("X-Relay-Key") or ""
    agent = store.agent_by_key(db, key)
    if agent is None:
        abort(401, "등록되지 않은 중계입니다. 다시 등록하세요.")
    return agent


@relay_api.post("/poll")
def relay_poll():
    """
    할 일을 받아 간다. 있으면 바로, 없으면 설정한 시간(기본 25초)까지 기다린다.

    기다리는 동안 gunicorn 스레드 하나를 잡는다. 그래서 길게 두지 않는다.
    """
    db = get_db()
    agent = _agent_or_401(db)
    store.touch_agent(db, agent["id"], auth.client_ip(), _client_scheme())
    # 새 판으로 바꿔 끼워도 등록을 다시 하지 않으므로, 판 번호는 여기서 맞춘다.
    m = re.match(r"claude-relay/(\d+\.\d+\.\d+)$", request.headers.get("User-Agent") or "")
    if m and m.group(1) != agent["version"]:
        db.execute("UPDATE relay_agents SET version = ? WHERE id = ?",
                   (m.group(1), agent["id"]))
    db.commit()

    wait_until = time.time() + store.poll_seconds(db)
    while True:
        # 기다리는 사이에 이 중계가 해제됐을 수 있다(다시 등록, 관리자가 끊음).
        # 키는 대기를 **시작할 때** 한 번 봤을 뿐이다. 해제된 중계의 롱폴이 새
        # 일이나 터널을 가져가면, 그 일은 아무도 처리하지 않는 곳으로 사라진다.
        if store.agent_revoked(db, agent["id"]):
            db.commit()
            abort(401, "등록이 해제된 중계입니다. 다시 등록하세요.")
        jobs = []
        for row in store.take_jobs(db, agent["id"], agent["owner_id"]):
            item = store.job_to_agent(db, row)
            if item is None:
                store.cancel_job(db, row["id"], "서버가 목록에서 사라졌습니다")
                db.commit()
                continue
            jobs.append(item)
        inputs = [{"term_id": tid, "data": data}
                  for tid, data in store.HUB.pending_input()]
        # 열어야 할 터널. 대상 주소는 DB 에 적힌 값이고 자격증명은 싣지 않는다.
        # SSH 는 사용자 PC 의 PuTTY 가 한다. 중계는 소켓만 연다.
        tunnels = tstore.HUB.take_dispatch(agent["owner_id"]) if agent["owner_id"] else []
        if jobs or inputs or tunnels or time.time() >= wait_until:
            store.touch_agent(db, agent["id"])
            db.commit()
            return jsonify(ok=True, jobs=jobs, input=inputs, tunnels=tunnels,
                           open_terms=store.HUB.open_ids(),
                           open_tunnels=[p.id for p in tstore.HUB.live()
                                         if p.agent_owner == agent["owner_id"]],
                           poll_seconds=store.poll_seconds(db))
        # 기다리기 전에 이 연결의 트랜잭션을 반드시 닫는다. 열린 쓰기 락을
        # 들고 25초를 기다리면 그 사이 다른 요청의 쓰기가 전부 막힌다.
        db.commit()
        store.wait_for_work(min(1.0, max(0.05, wait_until - time.time())))


@relay_api.post("/result")
def relay_result():
    """
    결과를 올린다. 한 번에 섞어 보낼 수 있다.

        {"jobs": [{"id": 1, "ok": true, "exit_code": 0, "output": "...",
                   "message": "...", "banner": "...", "elapsed": 0.42}],
         "term": [{"term_id": "...", "data": "...", "closed": false,
                   "reason": ""}]}

    터미널 화면(term[].data)은 DB 에 넣지 않는다. 메모리 버퍼로만 간다.
    """
    db = get_db()
    agent = _agent_or_401(db)
    store.touch_agent(db, agent["id"])
    data = request.get_json(silent=True) or {}

    done = 0
    for item in (data.get("jobs") or [])[:32]:
        try:
            job_id = int(item.get("id"))
        except (TypeError, ValueError):
            continue
        job = store.get_job(db, job_id)
        if job is None:
            continue
        ok = bool(item.get("ok"))
        result = {
            "exit_code": item.get("exit_code"),
            "message": str(item.get("message") or "")[:300],
            "banner": str(item.get("banner") or "")[:200],
            "elapsed": item.get("elapsed"),
            "error": str(item.get("error") or "")[:300],
        }
        # run 의 출력은 DB 에 적지 않는다. 기다리고 있는 요청 스레드에만
        # 메모리로 넘긴다. 출력은 그 대화에만 남아야 한다.
        if job["kind"] == store.KIND_RUN:
            store.put_side("output", job_id, str(item.get("output") or "")[:200000])
        if store.finish_job(db, job_id, ok, result, agent_id=agent["id"]):
            done += 1
        if job["kind"] == store.KIND_TERM_OPEN:
            store.set_term_state(db, job["term_id"], "open" if ok else "failed",
                                 result["error"] or result["message"])
            stream = store.HUB.get(job["term_id"])
            if stream and not ok:
                stream.close(result["error"] or result["message"] or "열지 못했습니다")
            if ok and job["server_id"]:
                store.touch_server_check(db, job["server_id"], True, "터미널 열림")
        if job["kind"] == store.KIND_TEST and job["server_id"]:
            store.touch_server_check(db, job["server_id"], ok,
                                     result["message"] or result["error"])

    for item in (data.get("term") or [])[:64]:
        term_id = str(item.get("term_id") or "")
        stream = store.HUB.get(term_id)
        if stream is None:
            continue
        chunk = item.get("data")
        if isinstance(chunk, str) and chunk:
            stream.push(chunk[:200000])
        if item.get("closed"):
            reason = str(item.get("reason") or "서버 쪽에서 연결이 끊어졌습니다")[:200]
            stream.close(reason)
            store.set_term_state(db, term_id, "closed", reason)
    db.commit()
    return jsonify(ok=True, accepted=done,
                   open_terms=store.HUB.open_ids())


@relay_api.post("/beat")
def relay_beat():
    """
    터미널이 열려 있는 동안 5초마다 온다. 화면에 "중계와 통신이 없습니다" 를
    띄울지 판단하는 기준이 이 신호다. (붙어 있다는 사실만 갱신한다)
    """
    db = get_db()
    agent = _agent_or_401(db)
    store.touch_agent(db, agent["id"])
    data = request.get_json(silent=True) or {}
    alive = [str(x) for x in (data.get("terms") or [])][:32]
    for term_id in alive:
        stream = store.HUB.get(term_id)
        if stream is not None:
            stream.last_agent = time.time()
    db.commit()
    return jsonify(ok=True, open_terms=store.HUB.open_ids(),
                   closed=[t for t in alive if store.HUB.get(t) is None
                           or store.HUB.get(t).state == "closed"])


# ---------------------------------------------------------------------------
# 터널 (중계 쪽)
#
# 중계는 소켓 하나를 열어 바이트를 옮길 뿐이다. 자격증명도 호스트 키도 모른다.
# 그 사람의 중계만 그 사람의 터널에 붙는다.
# ---------------------------------------------------------------------------
def _agent_pipe(db, agent, tunnel_id):
    pipe = tstore.HUB.get(tunnel_id)
    if pipe is None:
        abort(410, "닫힌 터널입니다.")
    if not agent["owner_id"] or pipe.agent_owner != agent["owner_id"]:
        abort(403, "이 중계의 터널이 아닙니다.")
    if pipe.agent_id is not None and pipe.agent_id != agent["id"]:
        abort(403, "다른 중계가 이미 맡은 터널입니다.")
    return pipe


@relay_api.post("/tunnel/<tunnel_id>/opened")
def relay_tunnel_opened(tunnel_id):
    """중계가 대상에 소켓을 열었는지(또는 왜 못 열었는지) 알린다."""
    db = get_db()
    agent = _agent_or_401(db)
    pipe = _agent_pipe(db, agent, tunnel_id)
    data = request.get_json(silent=True) or {}
    if data.get("ok"):
        pipe.mark_open(agent["id"])
        tstore.set_open(db, tunnel_id, agent["id"])
    else:
        # 여기 적히는 주소는 DB 에 있던 값이다. 요청에서 받은 주소를 쓰지
        # 않으므로 이 메시지로 내부망을 더듬을 수 없다.
        why = str(data.get("error") or "대상에 닿지 못했습니다")[:160]
        pipe.agent_id = agent["id"]
        tstore.close_tunnel(db, pipe, "VDI 중계가 %s:%s 에 닿지 못했습니다 (%s)"
                            % (pipe.target["host"], pipe.target["port"], why),
                            failed=True)
    db.commit()
    return jsonify(ok=True)


@relay_api.get("/tunnel/<tunnel_id>/down")
def relay_tunnel_down(tunnel_id):
    db = get_db()
    agent = _agent_or_401(db)
    pipe = _agent_pipe(db, agent, tunnel_id)
    if not pipe.attach("relay"):
        abort(409, "이 터널은 이미 다른 연결이 받고 있습니다.")
    db.commit()
    return Response(pipe.frames("relay"), mimetype="application/octet-stream",
                    headers={"X-Accel-Buffering": "no", "Cache-Control": "no-store"},
                    direct_passthrough=True)


@relay_api.post("/tunnel/<tunnel_id>/up")
def relay_tunnel_up(tunnel_id):
    import client_api
    db = get_db()
    agent = _agent_or_401(db)
    pipe = _agent_pipe(db, agent, tunnel_id)
    seq, chunk = client_api.read_chunk()
    db.commit()
    return client_api.put_result(db, pipe, pipe.put("relay", seq, chunk))


@relay_api.post("/tunnel/<tunnel_id>/close")
def relay_tunnel_close(tunnel_id):
    db = get_db()
    agent = _agent_or_401(db)
    pipe = _agent_pipe(db, agent, tunnel_id)
    data = request.get_json(silent=True) or {}
    tstore.close_tunnel(db, pipe, str(data.get("reason")
                                      or "대상 서버가 연결을 끊었습니다")[:160])
    db.commit()
    return jsonify(ok=True)


# ---------------------------------------------------------------------------
# 내 클라이언트 (브라우저가 부른다. 쿠키 + CSRF)
#
# 「내 중계」 와 같은 모양이다. 코드도 프로그램도 쓰는 사람이 직접 받는다.
# /api/client/* 가 아니라 여기 두는 이유: 저 접두어는 CSRF 를 건너뛴다.
# 브라우저가 부르는 길을 저기에 두면 남의 사이트가 대신 부를 수 있다.
# ---------------------------------------------------------------------------
def _my_client_payload(db, user):
    row = tstore.my_client(db, user["id"])
    prog = store.program_info("client")
    code = tstore.active_client_code(db, user["id"])
    return {
        "registered": row is not None,
        "connected": tstore.client_is_live(row),
        "name": row["name"] if row else "",
        "version": row["version"] if row else "",
        "last_seen_at": row["last_seen_at"] if row else None,
        "program": {"available": bool(prog), "name": prog.get("name") if prog else "",
                    "size": prog.get("size") if prog else 0,
                    "sha256": prog.get("sha256") if prog else ""},
        "enroll": ({"expires_at": code["expires_at"]} if code else None),
        "can_tunnel": permissions.can_open_shell(db, user, permissions.SHELL_TUNNEL),
        "server_url": request.url_root.rstrip("/"),
    }


@api.get("/my-client")
@auth.menu_required("servers")
def my_client():
    db, user = get_db(), _me()
    return jsonify(ok=True, client=_my_client_payload(db, user))


@api.post("/my-client/enroll")
@auth.menu_required("servers")
def my_client_enroll():
    """
    클라이언트 등록 코드. 원문은 **이 응답에만** 있다.

    터널 허용이 없는 사람에게는 주지 않는다. 클라이언트로 하는 일의 중심이
    터널이고, 허용이 생긴 뒤에 받으면 된다.
    """
    db, user = get_db(), _me()
    permissions.require_shell(db, user, permissions.SHELL_TUNNEL)
    code = tstore.new_client_code(db, user["id"])
    audit(db, user["id"], "client_enroll_code_issued", "client", "", "self ttl=600s")
    db.commit()
    return jsonify(ok=True, code=code, expires_in=config.RELAY_ENROLL_TTL_SECONDS,
                   server_url=request.url_root.rstrip("/"))


@api.post("/my-client/revoke")
@auth.menu_required("servers")
def my_client_revoke():
    db, user = get_db(), _me()
    row = tstore.my_client(db, user["id"])
    if row is None:
        abort(404, "등록된 내 클라이언트가 없습니다.")
    tstore.revoke_client(db, row["id"])
    n = tstore.close_user_tunnels(db, user["id"], "클라이언트 등록을 해제해 터널을 닫았습니다")
    audit(db, user["id"], "client_revoked", "client", row["id"], "self tunnels=%d" % n)
    db.commit()
    return jsonify(ok=True, tunnels_closed=n)


@bp.get("/servers/client-program")
@auth.menu_required("servers")
def download_client_program():
    """클라이언트 프로그램. 관리자가 올려 둔 파일 하나를 그대로 내려 준다."""
    db, user = get_db(), _me()
    meta = store.program_info("client")
    if meta is None:
        abort(404, "아직 클라이언트 프로그램이 올라와 있지 않습니다. 관리자에게 "
                   "요청하세요.")
    audit(db, user["id"], "client_program_downloaded", "client", "",
          "name=%s sha256=%s" % (meta["name"], meta["sha256"][:12]))
    db.commit()
    return send_file(meta["path"], as_attachment=True, download_name=meta["name"],
                     mimetype="application/octet-stream")


# ---------------------------------------------------------------------------
# 청소 스레드
# ---------------------------------------------------------------------------
def _housekeep_loop(app):
    from db import connect
    while True:
        time.sleep(10)
        try:
            conn = connect()
            try:
                store.housekeep(conn)
                import client_api
                tstore.housekeep(conn, client_api.still_allowed)
            finally:
                conn.close()
        except Exception:                      # pragma: no cover
            app.logger.exception("relay housekeep")


def start_housekeeping(app):
    """
    10초마다 도는 전용 스레드. 요청 스레드에서 하면 사람이 화면을 열 때마다
    시간이 달라지고, 아무도 안 들어온 밤에는 아무것도 정리되지 않는다.

    띄우기 전에 지난 프로세스가 남긴 터미널부터 닫는다.
    """
    from db import connect
    try:
        conn = connect()
        try:
            n = store.close_orphan_terms(conn)
            if n:
                app.logger.warning("지난 프로세스가 남긴 터미널 %d개를 닫았습니다", n)
            n = tstore.close_orphan_tunnels(conn)
            if n:
                app.logger.warning("지난 프로세스가 남긴 터널 %d개를 닫았습니다", n)
        finally:
            conn.close()
    except Exception:                          # pragma: no cover
        app.logger.exception("기동 시 터미널 정리")

    t = threading.Thread(target=_housekeep_loop, args=(app,),
                         name="relay-housekeep", daemon=True)
    t.start()
    return t
