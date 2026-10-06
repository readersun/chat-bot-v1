#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
client_api
==========

사용자 PC 의 클라이언트(claude-term)가 부르는 API.  /api/client/*

인증
----
쿠키를 쓰지 않는다. X-Client-Key 헤더로만 인증한다. 그래서 이 접두어는 CSRF
검사를 건너뛴다(app.py). 대신 **이 접두어 아래에서는 쿠키를 아예 읽지 않는다.**
쿠키로도 통하게 두면 남의 사이트가 사용자 브라우저를 시켜 CSRF 없이 이 길을
부를 수 있다.

키 하나가 그 사람 전체다. 그래서 쿠키 로그인과 같은 규칙으로 죽는다.

    - 계정이 정지되면                  401
    - 비밀번호가 바뀌면                 401 (등록할 때의 지문과 달라진다)
    - 웹에서 「등록 해제」 를 누르면     401
    - 다른 PC 에서 다시 등록하면        401 (한 사람에 한 대)

권한
----
키가 통해도 각 라우트는 웹과 **같은 검사**를 다시 한다. 메뉴 · 등급 · 범위 ·
셸 · 서버 켜짐 · 내 중계. 채팅 라우트는 웹의 뷰 함수를 그대로 다시 등록하므로
(app.py 의 add_url_rule) 검사도 한 벌이다.
"""

import base64
import binascii
import uuid

from flask import Blueprint, Response, abort, g, jsonify, request

import auth
import config
import permissions
import relay as relay_mod
import relay_store as rstore
import tunnel_store as tstore
from db import audit, get_db

PREFIX = "/api/client/"

client_api = Blueprint("client_api", __name__, url_prefix="/api/client")

STREAM_HEADERS = {
    # nginx 가 이 응답만 버퍼링하지 않게 한다. proxy_buffering 을 끄는 대신
    # 응답 하나에만 거는 표시다. 다른 응답은 지금처럼 버퍼링된다.
    "X-Accel-Buffering": "no",
    "Cache-Control": "no-store",
}
# text/plain 은 nginx gzip_types 에 들어 있다. gzip 은 모아서 압축하므로
# 스트림이 멈춘다. 바이트 스트림으로 내보낸다.
STREAM_MIMETYPE = "application/octet-stream"


# ---------------------------------------------------------------------------
# 인증 (app.py 의 before_request 가 부른다)
# ---------------------------------------------------------------------------
def load_client_user():
    """X-Client-Key 로 사람을 정한다. 쿠키는 보지 않는다."""
    g.user = None
    g.client = None
    key = request.headers.get("X-Client-Key") or ""
    if not key:
        return
    db = get_db()
    client = tstore.client_by_key(db, key)
    if client is None:
        return
    row = auth.user_by_id(db, client["owner_id"])
    if row is None or not row["is_active"]:
        return
    # 비밀번호가 바뀌면 이 키도 죽는다. 쿠키 로그인과 같은 규칙이다.
    if client["pw_stamp"] != auth._pw_stamp(row):
        return
    g.user = row
    g.client = client
    tstore.touch_client(db, client["id"], auth.client_ip(),
                        request.headers.get("X-Client-Version") or None)
    db.commit()


def _client_or_401():
    if getattr(g, "client", None) is None or g.user is None:
        abort(401, "이 클라이언트는 등록되어 있지 않거나 해제되었습니다. 웹의 서버 "
                   "화면 → 「내 클라이언트」 에서 새 코드를 받아 다시 등록하세요. "
                   "(비밀번호를 바꿨을 때도 다시 등록해야 합니다)")
    return g.client


# ---------------------------------------------------------------------------
# 등록
# ---------------------------------------------------------------------------
@client_api.post("/register")
def register():
    """
    등록. 코드는 그 사람이 웹에서 받은 것이다. 성공하면 키를 이 응답에만 준다.

    틀린 횟수는 중계 등록과 **같은 저울**로 센다(relay._enroll_blocked).
    코드가 6자리라 찍어 보는 것을 막아야 한다.
    """
    db = get_db()
    if relay_mod._enroll_blocked():
        abort(429, "등록 시도가 너무 많습니다. 10분 뒤에 다시 시도하세요.")
    data = request.get_json(silent=True) or {}
    got = tstore.consume_client_code(db, str(data.get("code") or ""))
    if got is None:
        db.commit()
        relay_mod._enroll_failed()
        audit(db, None, "client_register_failed", "client", "", "bad or expired code")
        db.commit()
        abort(403, "등록 코드가 틀렸거나 시간이 지났습니다. 웹의 서버 화면 → "
                   "「내 클라이언트」 에서 새 코드를 받아 주세요.")
    code_hash, owner_id = got
    owner = auth.user_by_id(db, owner_id)
    if owner is None or not owner["is_active"]:
        db.commit()
        abort(403, "등록 코드를 받은 계정을 쓸 수 없습니다.")
    cid, key = tstore.register_client(
        db, code_hash, owner_id, auth._pw_stamp(owner),
        name=data.get("name") or "", version=data.get("version") or "",
        os_info=data.get("os") or "", ip=auth.client_ip())
    audit(db, owner_id, "client_registered", "client", cid,
          "name=%s version=%s" % (data.get("name") or "", data.get("version") or ""))
    db.commit()
    return jsonify(ok=True, client_key=key, client_id=cid,
                   user=auth.public_user(owner)), 201


# ---------------------------------------------------------------------------
# 나와 서버 목록
# ---------------------------------------------------------------------------
@client_api.get("/me")
def me():
    client = _client_or_401()
    db, user = get_db(), g.user
    agent = rstore.live_agent(db, user["id"])
    return jsonify(
        ok=True,
        user=auth.public_user(user),
        client={"id": client["id"], "name": client["name"],
                "version": client["version"]},
        min_version=config.CLIENT_MIN_VERSION,
        version_ok=tstore.version_ok(request.headers.get("X-Client-Version")
                                     or client["version"]),
        menus=sorted(permissions.user_menus(db, user)),
        level=permissions.ssh_level(db, user),
        shell=permissions.shell_level(db, user),
        relay={"connected": agent is not None,
               "name": agent["name"] if agent else "",
               "last_seen_at": agent["last_seen_at"] if agent else None},
        # tab_* 은 1.1.0 부터 본다. shell_* 은 1.0.0 이 읽으므로 같은 값으로 남긴다.
        tab_max=tstore.tunnel_max_per_user(db),
        tab_open=tstore.open_tunnel_count(db, user["id"]),
        shell_max=tstore.tunnel_max_per_user(db),
        shell_open=tstore.open_tunnel_count(db, user["id"]))


@client_api.get("/servers")
def servers():
    """
    쓸 수 있는 서버. 거르는 자리는 SQL 이다(웹과 같은 visible_servers_clause).
    꺼진 서버는 오지 않는다. 비밀번호는 어떤 모양으로도 오지 않는다.
    """
    _client_or_401()
    db, user = get_db(), g.user
    permissions.require_menu(db, user, "servers")
    where, params = permissions.visible_servers_clause(db, user, "s")
    rows = db.execute("SELECT s.* FROM ssh_servers s WHERE " + where +
                      " AND s.is_enabled = 1 ORDER BY s.name", params).fetchall()
    open_by_server = {}
    for p in tstore.HUB.live():
        if p.user_id == user["id"]:
            open_by_server.setdefault(p.server_id, []).append(p.snapshot())
    out = []
    for r in rows:
        out.append({
            "id": r["id"], "name": r["name"], "username": r["username"],
            "address": "%s@%s:%s" % (r["username"], r["host"], r["port"]),
            "description": r["description"],
            "auth_label": rstore.AUTH_LABELS.get(r["auth_kind"], r["auth_kind"]),
            "last_check_ok": (None if r["last_check_ok"] is None
                              else bool(r["last_check_ok"])),
            "tunnels": open_by_server.get(r["id"], []),
        })
    return jsonify(ok=True, servers=out,
                   can_tunnel=permissions.can_open_shell(db, user,
                                                         permissions.SHELL_TUNNEL),
                   has_chat=permissions.has_menu(db, user, "chat"))


# ---------------------------------------------------------------------------
# 터널
# ---------------------------------------------------------------------------
@client_api.post("/tunnel")
def open_tunnel():
    """
    터널을 연다. 거절은 **모두 여기서** 한다. 클라이언트가 단추를 숨겨서 막는
    것이 아니다.

    대상은 DB 에 적힌 host:port 뿐이다. 요청에서 주소를 받지 않는다. 받으면
    챗봇 서버가 내부망으로 들어가는 아무 데나 붙여 주는 프록시가 된다.
    """
    client = _client_or_401()
    db, user = get_db(), g.user
    version = request.headers.get("X-Client-Version") or client["version"]
    if not tstore.version_ok(version):
        # 426 Upgrade Required. werkzeug 의 abort 가 이 코드를 모르므로 직접 돌려준다.
        return jsonify(ok=False, upgrade=True, min_version=config.CLIENT_MIN_VERSION,
                       error="이 클라이언트가 낡았습니다. 서버가 %s 이상을 요구합니다. "
                             "웹의 서버 화면에서 새로 받아 주세요."
                             % config.CLIENT_MIN_VERSION), 426
    permissions.require_menu(db, user, "servers")
    data = request.get_json(silent=True) or {}
    try:
        server_id = int(data.get("server_id") or 0)
    except (TypeError, ValueError):
        abort(400, "서버를 고르세요.")
    row = rstore.get_server(db, server_id)
    permissions.require_server(db, user, row)
    if not row["is_enabled"]:
        abort(409, "%s 서버는 지금 꺼져 있습니다. 다른 서버를 고르거나 관리자에게 "
                   "문의하세요." % row["name"])
    permissions.require_shell(db, user, permissions.SHELL_TUNNEL)
    relay_mod._require_my_relay(db, user)

    limit = tstore.tunnel_max_per_user(db)
    if tstore.open_tunnel_count(db, user["id"]) >= limit:
        abort(409, "PuTTY 탭은 한 사람에 %d개까지입니다. 쓰지 않는 탭을 먼저 닫아 "
                   "주세요. (관리자가 중계 설정에서 바꿀 수 있습니다)" % limit)
    if not tstore.capacity_left(db):
        abort(429, "지금 터널을 더 열 수 없습니다. 잠시 뒤에 다시 시도해 주세요.")

    tunnel_id = uuid.uuid4().hex
    target = {"host": row["host"], "port": int(row["port"] or 22)}
    tstore.open_tunnel_row(db, tunnel_id, server_id, user["id"], client["id"],
                           "%s:%s" % (target["host"], target["port"]), auth.client_ip())
    tstore.HUB.create(tunnel_id, server_id, user["id"], user["id"], client["id"], target)
    audit(db, user["id"], "tunnel_opened", "ssh_server", server_id,
          "name=%s tunnel=%s" % (row["name"], tunnel_id[:8]))
    db.commit()
    rstore.wake()
    return jsonify(ok=True, tunnel_id=tunnel_id,
                   server={"id": row["id"], "name": row["name"],
                           "username": row["username"]},
                   heartbeat=config.TUNNEL_HEARTBEAT_SECONDS), 201


def _my_pipe(tunnel_id):
    """내 터널만. 관리자도 남의 터널에 붙지 못한다."""
    client = _client_or_401()
    pipe = tstore.HUB.get(tunnel_id)
    if pipe is None:
        row = tstore.tunnel_row(get_db(), tunnel_id)
        if row is not None and row["user_id"] == g.user["id"]:
            abort(410, row["close_reason"] or "닫힌 터널입니다.")
        abort(404, "터널을 찾을 수 없습니다.")
    if pipe.user_id != g.user["id"] or pipe.client_id != client["id"]:
        abort(403, "내가 연 터널만 쓸 수 있습니다.")
    return pipe


@client_api.get("/tunnel/<tunnel_id>/down")
def tunnel_down(tunnel_id):
    pipe = _my_pipe(tunnel_id)
    if not pipe.attach("client"):
        abort(409, "이 터널은 이미 다른 연결이 받고 있습니다.")
    # 스트림 동안 DB 를 쥐고 있지 않는다. 응답을 돌려주는 순간 요청 문맥이
    # 끝나고 연결이 닫힌다. 제너레이터는 메모리만 본다.
    get_db().commit()
    return Response(pipe.frames("client"), mimetype=STREAM_MIMETYPE,
                    headers=STREAM_HEADERS, direct_passthrough=True)


def read_chunk():
    data = request.get_json(silent=True) or {}
    try:
        seq = int(data.get("seq"))
    except (TypeError, ValueError):
        abort(400, "순번이 없습니다.")
    raw = data.get("data") or ""
    if not isinstance(raw, str) or len(raw) > tstore.MAX_CHUNK * 4 // 3 + 8:
        abort(413, "조각이 너무 큽니다.")
    try:
        chunk = base64.b64decode(raw.encode("ascii"), validate=True)
    except (binascii.Error, ValueError, UnicodeEncodeError):
        abort(400, "조각을 읽지 못했습니다.")
    return seq, chunk


def put_result(db, pipe, verdict):
    """Pipe.put 의 결과를 HTTP 로 옮긴다. 라우트 둘(클라이언트·중계)이 같이 쓴다."""
    if verdict in ("ok", "dup"):
        return jsonify(ok=True, dup=verdict == "dup")
    if verdict == "busy":
        abort(503, "반대편이 따라오지 못합니다. 같은 순번으로 다시 보내세요.")
    if verdict == "gap":
        tstore.close_tunnel(db, pipe, "조각의 순서가 어긋나 닫았습니다")
        db.commit()
        abort(409, "조각의 순서가 어긋나 터널을 닫았습니다.")
    snap = pipe.snapshot()
    abort(410, snap["reason"] or "닫힌 터널입니다.")


@client_api.post("/tunnel/<tunnel_id>/up")
def tunnel_up(tunnel_id):
    pipe = _my_pipe(tunnel_id)
    seq, chunk = read_chunk()
    db = get_db()
    db.commit()             # 기다릴 수 있다. 쓰기 락을 들고 기다리지 않는다
    return put_result(db, pipe, pipe.put("client", seq, chunk))


@client_api.post("/tunnel/<tunnel_id>/close")
def tunnel_close(tunnel_id):
    pipe = _my_pipe(tunnel_id)
    db = get_db()
    data = request.get_json(silent=True) or {}
    why = str(data.get("reason") or "사용자가 닫았습니다")[:120]
    tstore.close_tunnel(db, pipe, why)
    audit(db, g.user["id"], "tunnel_closed", "ssh_server", pipe.server_id,
          "tunnel=%s up=%d down=%d" % (tunnel_id[:8], pipe.bytes_up, pipe.bytes_down))
    db.commit()
    return jsonify(ok=True, closed=tunnel_id)


@client_api.get("/tunnels")
def my_tunnels():
    """최근 터널. 닫힌 것도 이유와 함께 나온다(왜 끊겼는지를 PuTTY 는 모른다)."""
    _client_or_401()
    db = get_db()
    rows = tstore.tunnels_for_user(db, g.user["id"])
    out = []
    for r in rows:
        pipe = tstore.HUB.get(r["id"])
        snap = pipe.snapshot() if pipe else None
        out.append({"tunnel_id": r["id"], "server_id": r["server_id"],
                    "server_name": r["server_name"] or "(삭제된 서버)",
                    "state": snap["state"] if snap else r["state"],
                    "reason": (snap["reason"] if snap else "") or r["close_reason"],
                    "opened_at": r["opened_at"], "closed_at": r["closed_at"],
                    "bytes_up": snap["bytes_up"] if snap else r["bytes_up"],
                    "bytes_down": snap["bytes_down"] if snap else r["bytes_down"]})
    return jsonify(ok=True, tunnels=out)


# ---------------------------------------------------------------------------
# 청소 때 다시 보는 권한
# ---------------------------------------------------------------------------
def still_allowed(db, pipe):
    """열려 있는 터널의 주인이 지금도 그 터널을 가질 수 있는가. 아니면 이유."""
    user = auth.user_by_id(db, pipe.user_id)
    if user is None or not user["is_active"]:
        return "계정을 쓸 수 없게 되어 터널을 닫았습니다"
    server = rstore.get_server(db, pipe.server_id)
    if server is None:
        return "서버가 목록에서 빠져 터널을 닫았습니다"
    if not server["is_enabled"]:
        return "관리자가 %s 서버를 꺼서 터널을 닫았습니다" % server["name"]
    if (not permissions.has_menu(db, user, "servers")
            or not permissions.can_use_server(db, user, server)):
        return "%s 를 쓸 허용이 해제되어 터널을 닫았습니다" % server["name"]
    if not permissions.can_open_shell(db, user, permissions.SHELL_TUNNEL):
        return "PuTTY 터널 허용이 해제되어 터널을 닫았습니다"
    client = db.execute("SELECT * FROM client_agents WHERE id = ?",
                        (pipe.client_id,)).fetchone()
    if client is None or client["revoked_at"] or client["pw_stamp"] != auth._pw_stamp(user):
        return "클라이언트 등록이 해제되어 터널을 닫았습니다"
    if pipe.state == "open" and rstore.live_agent(db, pipe.user_id) is None:
        return "VDI 중계와 통신이 끊어져 터널을 닫았습니다"
    return ""
