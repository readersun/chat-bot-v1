#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
patch
=====

패치 저장소의 사용자 쪽. 보는 것과 받는 것 둘뿐이다. 올리는 것은 scp 로 하고
색인은 patch_scan 이 만든다. 이 파일에는 쓰기가 하나도 없다.

권한
----
모든 라우트에 menu_required("patch") 가 걸려 있다. 레일에서 항목을 안 그리는
것은 장식이고, 실제로 막는 곳은 여기다. API 하나를 빼먹으면 권한 없는 사람이
curl 한 줄로 목록을 받아 간다.

그 위에 사이트 범위가 한 겹 더 있다. 고객사가 여럿이면 patch 메뉴 하나로
남의 고객사 패치까지 보이면 안 된다. 거르는 곳은 목록 질의와 다운로드 두
곳이고, 목록에 안 보이는 것은 다운로드도 404 다.

다운로드
--------
Flask 는 권한만 판정하고 전송은 nginx 에 넘긴다(X-Accel-Redirect). 이 앱은
워커 1개 / 스레드 8개로 돌아서, Flask 가 3GB tar 를 들고 부르면 다운로드
여덟 개가 스레드를 전부 잡아 채팅과 로그인까지 멈춘다. 이어받기(Range)도
nginx 가 공짜로 해 준다.

경로는 클라이언트에서 받지 않는다. 파일 id 하나만 받고 경로는 DB 에서 만든다.
만든 뒤에도 realpath 가 루트 안인지 다시 확인한다(심볼릭 링크 포함).
"""

import os
import urllib.parse

from flask import (
    Blueprint, abort, jsonify, render_template, request, send_file,
)

import auth
import config
import patch_rules
import permissions
from db import audit, get_db

bp = Blueprint("patch", __name__)
api = Blueprint("patch_api", __name__, url_prefix="/api/patch")


# ---------------------------------------------------------------------------
# 경로
# ---------------------------------------------------------------------------
def inside(path, root):
    """심볼릭 링크를 따라간 실제 경로가 root 안인가."""
    try:
        real = os.path.realpath(path)
        base = os.path.realpath(root)
        return os.path.commonpath([real, base]) == base
    except (ValueError, OSError):
        return False


def file_abs_path(root_path, rel_path):
    """
    DB 의 rel_path 로 실제 경로를 만든다. rel_path 는 스캔이 넣은 값이지만
    한 번 더 검사한다. DB 가 오염돼도 루트 밖으로 못 나가게 한다.
    """
    parts = [p for p in str(rel_path or "").split("/") if p]
    if not parts or not all(patch_rules.safe_segment(p) for p in parts):
        return None
    full = os.path.join(root_path, *parts)
    if not inside(full, root_path):
        return None
    # 루트 자체가 울타리 안인지도 본다. 관리자가 루트를 등록할 때 이미
    # 검사하지만, 설정이 바뀐 뒤를 대비해 내보내기 직전에 다시 확인한다.
    if not inside(full, config.PATCH_BASE_DIR):
        return None
    return full


# ---------------------------------------------------------------------------
# 조회 helper
# ---------------------------------------------------------------------------
def _sites_where(db, user, alias="s"):
    return permissions.visible_sites_clause(db, user, alias)


def visible_product(db, user, pid):
    """볼 수 있는 제품 라인 행. 없거나 권한 밖이면 404."""
    where, params = _sites_where(db, user)
    row = db.execute(
        "SELECT p.*, s.name AS site_name, s.date_format AS date_format,"
        "       s.id AS site_id, r.path AS root_path, r.id AS root_id"
        "  FROM patch_products p"
        "  JOIN patch_sites s ON s.id = p.site_id"
        "  JOIN patch_roots r ON r.id = s.root_id"
        " WHERE p.id = ? AND p.is_visible = 1 AND " + where,
        [pid] + params).fetchone()
    if row is None:
        abort(404, "제품 라인을 찾을 수 없습니다.")
    return row


def file_payload(row):
    return {
        "id": row["id"],
        "module": row["module_name"],
        "module_label": row["module_label"] or "",
        "version": row["version"],
        "suffix": row["suffix"],
        "date_dir": row["date_dir"],
        "date_at": row["date_at"],
        "filename": row["filename"],
        "size": row["size"],
        "sha256": row["sha256"] or "",
        "is_missing": bool(row["is_missing"]),
        "content_changed": bool(row["content_changed"]),
        "download_url": "/api/patch/files/%d/download" % row["id"],
    }


# ---------------------------------------------------------------------------
# 화면
# ---------------------------------------------------------------------------
@bp.get("/patch")
@auth.menu_required("patch")
def page():
    user = auth.current_user()
    db = get_db()
    return render_template(
        "patch.html",
        csrf=auth.csrf_token(),
        me=auth.public_user(user),
        is_admin=permissions.is_admin(user),
        menus=sorted(permissions.user_menus(db, user)),
        rail="patch",
    )


# ---------------------------------------------------------------------------
# 목록 API
# ---------------------------------------------------------------------------
@api.get("/tree")
@auth.menu_required("patch")
def tree():
    """
    왼쪽 선택 칸이 쓰는 사이트 → 제품 라인 트리.

    숨긴 것과 범위 밖은 애초에 나오지 않는다. 루트의 절대 경로는 돌려주지
    않는다. 사용자는 DAS 경로를 알 필요가 없다. 같은 이름의 사이트가 서로
    다른 루트에 있을 때만 구별용으로 루트 '이름' 을 함께 준다.
    """
    db, user = get_db(), auth.current_user()
    where, params = _sites_where(db, user)
    rows = db.execute(
        "SELECT s.id AS site_id, s.name AS site_name, s.label AS site_label,"
        "       r.label AS root_label, r.id AS root_id,"
        "       p.id AS product_id, p.name AS product_name, p.label AS product_label"
        "  FROM patch_sites s"
        "  JOIN patch_roots r ON r.id = s.root_id"
        "  LEFT JOIN patch_products p ON p.site_id = s.id AND p.is_visible = 1"
        " WHERE " + where +
        " ORDER BY s.name, p.sort_order, p.name", params).fetchall()

    sites, order = {}, []
    for r in rows:
        sid = r["site_id"]
        if sid not in sites:
            sites[sid] = {"id": sid, "name": r["site_name"],
                          "label": r["site_label"] or "",
                          "root_label": r["root_label"] or "",
                          "root_id": r["root_id"], "products": []}
            order.append(sid)
        if r["product_id"]:
            sites[sid]["products"].append(
                {"id": r["product_id"], "name": r["product_name"],
                 "label": r["product_label"] or ""})

    out = [sites[i] for i in order]
    # 같은 이름의 사이트가 둘 이상이면 화면에서 루트 이름을 함께 보여 줘야 한다
    seen = {}
    for s in out:
        seen[s["name"]] = seen.get(s["name"], 0) + 1
    for s in out:
        s["ambiguous"] = seen[s["name"]] > 1

    last = db.execute(
        "SELECT MAX(last_scanned_at) AS t FROM patch_roots WHERE scan_enabled = 1"
    ).fetchone()
    return jsonify(ok=True, sites=out, last_scanned_at=last["t"] if last else None)


@api.get("/dates")
@auth.menu_required("patch")
def dates():
    db, user = get_db(), auth.current_user()
    product = visible_product(db, user, request.args.get("product_id", type=int))
    # 건수는 "사용자가 실제로 받을 수 있는 것" 이어야 한다. 매칭 안 된 파일,
    # 숨긴 파일, 숨긴 모듈의 파일은 세지 않는다. 세어 버리면 날짜를 눌렀는데
    # 목록이 비는 일이 생긴다.
    rows = db.execute(
        "SELECT f.date_dir AS date_dir, MAX(f.date_at) AS date_at, COUNT(*) AS n,"
        "       SUM(CASE WHEN m.id IS NULL THEN 0 ELSE 1 END) AS matched"
        "  FROM patch_files f"
        "  LEFT JOIN patch_modules m ON m.id = f.module_id AND m.is_visible = 1"
        " WHERE f.product_id = ? AND f.is_visible = 1"
        " GROUP BY f.date_dir"
        # 날짜를 못 읽은 폴더는 버리지 않고 맨 뒤로 보낸다
        " ORDER BY (date_at IS NULL), date_at DESC, f.date_dir DESC",
        (product["id"],)).fetchall()
    return jsonify(ok=True, dates=[
        {"date_dir": r["date_dir"], "date_at": r["date_at"], "count": r["matched"]}
        for r in rows if r["matched"]])


@api.get("/files")
@auth.menu_required("patch")
def files():
    """
    latest=1 이면 모듈마다 가장 높은 버전 한 건씩만 돌린다.
    정렬은 version_sort 로 한다. 1.2.14 가 1.2.9 보다 위여야 한다.

    매칭 안 된 파일(module_id IS NULL)은 사용자 목록에 나오지 않는다.
    그것은 관리자가 모듈을 등록하라고 보는 목록이다.

    관리자가 끈 것도 나오지 않는다. 모듈을 끄면 그 모듈의 파일 전부가,
    파일 한 건을 끄면 그것만 사라진다. 거르는 곳이 목록과 다운로드 두 곳
    뿐이어야 하므로 두 조건을 같은 모양으로 써 둔다.
    """
    db, user = get_db(), auth.current_user()
    product = visible_product(db, user, request.args.get("product_id", type=int))

    sql = ["SELECT f.*, m.name AS module_name, m.label AS module_label",
           "  FROM patch_files f",
           "  JOIN patch_modules m ON m.id = f.module_id AND m.is_visible = 1",
           " WHERE f.product_id = ? AND f.is_visible = 1"]
    params = [product["id"]]

    date_dir = (request.args.get("date") or "").strip()
    if date_dir:
        sql.append(" AND f.date_dir = ?")
        params.append(date_dir)

    q = (request.args.get("q") or "").strip()
    if q:
        sql.append(" AND (m.name LIKE ? OR m.label LIKE ?)")
        params.extend(["%" + q + "%", "%" + q + "%"])

    if request.args.get("latest") in ("1", "true", "yes"):
        sql.append(
            # f2 에도 is_visible 을 걸어야 한다. 빼면 "가장 높은 버전" 이
            # 숨긴 파일로 뽑히고, 바깥 조건이 그 줄을 떨어뜨려서 보이는 옛
            # 버전이 있는데도 모듈이 통째로 사라진다.
            " AND f.id = (SELECT f2.id FROM patch_files f2"
            "              WHERE f2.product_id = f.product_id"
            "                AND f2.module_id = f.module_id"
            "                AND f2.is_visible = 1"
            "              ORDER BY f2.version_sort DESC, f2.date_dir DESC LIMIT 1)")

    sql.append(" ORDER BY m.sort_order, m.name, f.version_sort DESC, f.date_dir DESC")
    rows = db.execute("".join(sql), params).fetchall()
    return jsonify(ok=True, files=[file_payload(r) for r in rows],
                   product={"id": product["id"], "name": product["name"],
                            "label": product["label"] or "",
                            "site": product["site_name"]})


# ---------------------------------------------------------------------------
# 다운로드
# ---------------------------------------------------------------------------
def _content_disposition(filename):
    """
    RFC 5987. 파일 이름은 보통 ASCII 지만 규칙이 깨진 파일이 들어올 수 있고,
    그때 헤더가 깨지면 다운로드 자체가 실패한다.
    """
    quoted = urllib.parse.quote(filename, safe="")
    ascii_name = filename.encode("ascii", "replace").decode("ascii").replace('"', "_")
    return 'attachment; filename="%s"; filename*=UTF-8\'\'%s' % (ascii_name, quoted)


@api.get("/files/<int:fid>/download")
@auth.menu_required("patch")
def download(fid):
    db, user = get_db(), auth.current_user()

    where, params = _sites_where(db, user)
    row = db.execute(
        "SELECT f.*, m.name AS module_name, s.name AS site_name, s.id AS site_id,"
        "       p.name AS product_name, r.path AS root_path"
        "  FROM patch_files f"
        "  JOIN patch_products p ON p.id = f.product_id"
        "  JOIN patch_sites s ON s.id = p.site_id"
        "  JOIN patch_roots r ON r.id = s.root_id"
        # LEFT 가 아니라 JOIN 이다. 매칭 안 된 파일과 숨긴 모듈의 파일은
        # 목록에 없으니 다운로드도 없어야 한다. 목록에만 걸고 여기를 빼면
        # id 를 아는 사람이 그대로 받아 간다.
        "  JOIN patch_modules m ON m.id = f.module_id AND m.is_visible = 1"
        " WHERE f.id = ? AND f.is_visible = 1 AND p.is_visible = 1 AND " + where,
        [fid] + params).fetchone()

    # 목록에 안 보이는 것은 다운로드도 404 다. 403 으로 "있긴 있다" 를
    # 알려 주지 않는다.
    if row is None:
        abort(404, "파일을 찾을 수 없습니다.")
    if row["is_missing"]:
        abort(404, "파일이 더 이상 없습니다. 관리자에게 알려 주세요.")

    full = file_abs_path(row["root_path"], row["rel_path"])
    if full is None or not os.path.isfile(full):
        abort(404, "파일을 찾을 수 없습니다.")

    # 누가 언제 어떤 모듈 몇 버전을 받았는지. 어떤 버전이 어디 나갔는지
    # 추적할 수 없다는 문제가 실제로 풀리는 지점은 화면이 아니라 이 로그다.
    audit(db, user["id"], "patch.download", "patch_file", row["id"],
          "%s/%s %s %s" % (row["site_name"], row["product_name"],
                           row["module_name"] or "-", row["version"]))
    db.commit()

    if config.PATCH_XACCEL:
        # 본문은 한 바이트도 읽지 않는다. 헤더만 돌려주고 빠진다.
        rel = os.path.relpath(os.path.realpath(full),
                              os.path.realpath(config.PATCH_BASE_DIR))
        target = config.PATCH_XACCEL_PREFIX + rel.replace(os.sep, "/")
        resp = jsonify(ok=True)
        resp.headers["X-Accel-Redirect"] = urllib.parse.quote(target)
        resp.headers["Content-Type"] = "application/x-tar"
        resp.headers["Content-Disposition"] = _content_disposition(row["filename"])
        resp.headers["Content-Length"] = str(row["size"])
        return resp

    # nginx 가 없는 개발 PC 용. conditional=True 면 Range 를 직접 처리한다.
    return send_file(full, mimetype="application/x-tar", as_attachment=True,
                     download_name=row["filename"], conditional=True)
