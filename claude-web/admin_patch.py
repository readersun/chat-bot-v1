#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
admin_patch
===========

패치 저장소의 관리자 쪽. 전부 @admin_required 다.

손으로 등록하는 것은 **와칭 루트뿐이다.**
사이트와 제품 라인은 스캔이 찾아 넣고, 여기서는 표시 이름과 공개 여부, 날짜
형식만 바꾼다. 그래서 두 표에는 POST 와 DELETE 가 없다.

모듈만은 등록한다. "매칭이 되어 있는 파일만 버전 관리" 라는 규칙이 거기 걸려
있다. 사이트가 늘면 제품 라인마다 다시 등록해야 하므로 두 가지 도우미를 둔다.
    - 다른 제품 라인에서 통째로 복사
    - 매칭 안 된 파일에서 골라 한 번에 등록

지우는 것에 대하여
------------------
어느 DELETE 도 DAS 의 파일을 건드리지 않는다. 모듈을 지우면 그 모듈로
매칭됐던 행의 module_id 가 NULL 이 되어 "매칭 안 된 파일" 로 돌아갈 뿐이고,
루트를 지우면 색인은 사라져도 파일은 그대로 남아 다시 등록하고 한 번
스캔하면 전부 돌아온다. 삭제가 무섭지 않은 구조다.
"""

import os

from flask import Blueprint, abort, jsonify, render_template, request

import config
import patch_rules
import patch_scan
import permissions
from auth import admin_required, csrf_token, current_user, public_user
from db import audit, get_db, ts

bp = Blueprint("admin_patch", __name__, url_prefix="/admin")
api = Blueprint("admin_patch_api", __name__, url_prefix="/api/admin/patch")


# ---------------------------------------------------------------------------
# 화면
# ---------------------------------------------------------------------------
@bp.get("/patch")
@admin_required
def page():
    user = current_user()
    return render_template(
        "admin_patch.html",
        csrf=csrf_token(),
        page_title="패치 설정",
        me=public_user(user),
        is_admin=True,
        menus=sorted(permissions.MENUS),
        rail="patch_admin",
    )


# ---------------------------------------------------------------------------
# 공통
# ---------------------------------------------------------------------------
def _body():
    return request.get_json(silent=True) or {}


def _row_or_404(db, table, rid, what):
    row = db.execute("SELECT * FROM %s WHERE id = ?" % table, (rid,)).fetchone()
    if row is None:
        abort(404, "%s을(를) 찾을 수 없습니다." % what)
    return row


def _check_path(path):
    """(ok, 사람이 읽을 메시지). 루트를 저장하기 전에 통과해야 한다."""
    path = (path or "").strip().rstrip("/\\")
    if not path:
        return False, "경로를 입력해 주세요."
    if not os.path.isabs(path):
        return False, "절대 경로로 적어 주세요. 예: /mnt/das/patch"

    # 울타리 검사. 다운로드는 nginx 가 PATCH_BASE_DIR 아래만 열어 두므로,
    # 밖에 있는 루트를 등록하면 목록에는 뜨는데 받을 수는 없는 상태가 된다.
    base = os.path.realpath(config.PATCH_BASE_DIR)
    try:
        real = os.path.realpath(path)
        inside = os.path.commonpath([real, base]) == base
    except (ValueError, OSError):
        inside = False
    if not inside:
        return False, ("%s 아래에 있어야 합니다. (지금: %s)\n"
                       "다운로드는 nginx 가 이 디렉터리 아래만 내보냅니다."
                       % (config.PATCH_BASE_DIR, path))

    if not os.path.isdir(path):
        return False, "그런 디렉터리가 없습니다. 마운트를 확인해 주세요: %s" % path
    try:
        entries = os.listdir(path)
    except OSError as exc:
        return False, "읽을 수 없습니다: %s" % exc
    subdirs = [e for e in entries if os.path.isdir(os.path.join(path, e))]
    if not subdirs:
        return False, "폴더가 하나도 없습니다. 마운트가 빠졌는지 확인해 주세요."
    return True, "확인했습니다. 사이트 후보 %d개가 보입니다: %s" % (
        len(subdirs), ", ".join(sorted(subdirs)[:5]))


# ---------------------------------------------------------------------------
# 전체 현황 (화면이 한 번에 읽는다)
# ---------------------------------------------------------------------------
@api.get("/overview")
@admin_required
def overview():
    db = get_db()
    roots = []
    for r in db.execute("SELECT * FROM patch_roots ORDER BY id").fetchall():
        d = {k: r[k] for k in r.keys()}
        d["site_count"] = db.execute(
            "SELECT COUNT(*) c FROM patch_sites WHERE root_id = ?", (r["id"],)
        ).fetchone()["c"]
        roots.append(d)

    sites = []
    for s in db.execute(
        "SELECT s.*, r.path AS root_path, r.label AS root_label"
        "  FROM patch_sites s JOIN patch_roots r ON r.id = s.root_id"
        " ORDER BY r.id, s.name").fetchall():
        d = {k: s[k] for k in s.keys()}
        d["product_count"] = db.execute(
            "SELECT COUNT(*) c FROM patch_products WHERE site_id = ?", (s["id"],)
        ).fetchone()["c"]
        sites.append(d)

    products = [{k: p[k] for k in p.keys()} for p in db.execute(
        "SELECT p.*, s.name AS site_name, s.root_id AS root_id"
        "  FROM patch_products p JOIN patch_sites s ON s.id = p.site_id"
        " ORDER BY s.name, p.sort_order, p.name").fetchall()]
    for p in products:
        p["module_count"] = db.execute(
            "SELECT COUNT(*) c FROM patch_modules WHERE product_id = ?", (p["id"],)
        ).fetchone()["c"]
        p["unmatched_count"] = db.execute(
            "SELECT COUNT(*) c FROM patch_files"
            " WHERE product_id = ? AND module_id IS NULL AND is_missing = 0",
            (p["id"],)).fetchone()["c"]
        # 어디를 꺼 뒀는지 한눈에 보여야 한다. 끈 것을 잊으면 "파일은 올렸는데
        # 사용자가 못 받는다" 를 DAS 부터 뒤지게 된다.
        p["hidden_module_count"] = db.execute(
            "SELECT COUNT(*) c FROM patch_modules WHERE product_id = ? AND is_visible = 0",
            (p["id"],)).fetchone()["c"]
        p["hidden_file_count"] = db.execute(
            "SELECT COUNT(*) c FROM patch_files"
            " WHERE product_id = ? AND is_visible = 0 AND is_missing = 0",
            (p["id"],)).fetchone()["c"]

    rules = [{k: r[k] for k in r.keys()} for r in db.execute(
        "SELECT * FROM patch_version_rules ORDER BY is_builtin DESC, name").fetchall()]

    return jsonify(ok=True, roots=roots, sites=sites, products=products,
                   rules=rules, base_dir=config.PATCH_BASE_DIR,
                   date_presets=list(patch_rules.DATE_PRESETS),
                   xaccel=bool(config.PATCH_XACCEL))


# ---------------------------------------------------------------------------
# 와칭 루트
# ---------------------------------------------------------------------------
@api.post("/roots")
@admin_required
def add_root():
    db, data = get_db(), _body()
    path = (data.get("path") or "").strip().rstrip("/\\")
    ok, msg = _check_path(path)
    if not ok:
        abort(400, msg)
    fmt = (data.get("date_format") or patch_rules.DEFAULT_DATE_FORMAT).strip()
    ok, err = patch_rules.validate_date_format(fmt)
    if not ok:
        abort(400, err)
    if db.execute("SELECT 1 FROM patch_roots WHERE path = ?", (path,)).fetchone():
        abort(400, "이미 등록된 경로입니다.")

    now = ts()
    cur = db.execute(
        "INSERT INTO patch_roots (label, path, scan_interval_s, date_format,"
        " hash_enabled, created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
        ((data.get("label") or "").strip()[:100], path,
         max(60, int(data.get("scan_interval_s") or 600)), fmt,
         1 if data.get("hash_enabled") else 0, now, now))
    audit(db, current_user()["id"], "patch.root_added", "patch_root", cur.lastrowid, path)
    db.commit()
    return jsonify(ok=True, id=cur.lastrowid, message=msg), 201


@api.patch("/roots/<int:rid>")
@admin_required
def update_root(rid):
    db, data = get_db(), _body()
    _row_or_404(db, "patch_roots", rid, "와칭 루트")
    fields, values = [], []

    if "label" in data:
        fields.append("label = ?"); values.append((data.get("label") or "").strip()[:100])
    if "scan_enabled" in data:
        fields.append("scan_enabled = ?"); values.append(1 if data["scan_enabled"] else 0)
    if "hash_enabled" in data:
        fields.append("hash_enabled = ?"); values.append(1 if data["hash_enabled"] else 0)
    if "scan_interval_s" in data:
        fields.append("scan_interval_s = ?")
        values.append(max(60, int(data.get("scan_interval_s") or 600)))
    if "date_format" in data:
        fmt = (data.get("date_format") or "").strip()
        ok, err = patch_rules.validate_date_format(fmt)
        if not ok:
            abort(400, err)
        fields.append("date_format = ?"); values.append(fmt)
    if "path" in data:
        path = (data.get("path") or "").strip().rstrip("/\\")
        ok, msg = _check_path(path)
        if not ok:
            abort(400, msg)
        other = db.execute("SELECT 1 FROM patch_roots WHERE path = ? AND id != ?",
                           (path, rid)).fetchone()
        if other:
            abort(400, "이미 등록된 경로입니다.")
        fields.append("path = ?"); values.append(path)

    if not fields:
        abort(400, "바꿀 내용이 없습니다.")
    fields.append("updated_at = ?"); values.append(ts()); values.append(rid)
    db.execute("UPDATE patch_roots SET %s WHERE id = ?" % ", ".join(fields), values)
    audit(db, current_user()["id"], "patch.root_updated", "patch_root", rid,
          ",".join(sorted(k for k in data)))
    db.commit()
    return jsonify(ok=True)


@api.delete("/roots/<int:rid>")
@admin_required
def delete_root(rid):
    db = get_db()
    row = _row_or_404(db, "patch_roots", rid, "와칭 루트")
    db.execute("DELETE FROM patch_roots WHERE id = ?", (rid,))
    audit(db, current_user()["id"], "patch.root_deleted", "patch_root", rid, row["path"])
    db.commit()
    # 파일은 그대로 있다. 다시 등록하고 한 번 스캔하면 색인이 돌아온다.
    return jsonify(ok=True, deleted=rid, note="색인만 지웠습니다. DAS 의 파일은 그대로입니다.")


@api.post("/roots/check")
@admin_required
def check_root_path():
    ok, msg = _check_path((_body().get("path") or ""))
    return jsonify(ok=ok, message=msg)


# ---------------------------------------------------------------------------
# 사이트 / 제품 라인 (스캔이 찾아 넣는다. 여기서는 고르기만 한다)
# ---------------------------------------------------------------------------
@api.patch("/sites/<int:sid>")
@admin_required
def update_site(sid):
    db, data = get_db(), _body()
    _row_or_404(db, "patch_sites", sid, "사이트")
    fields, values = [], []
    if "label" in data:
        fields.append("label = ?"); values.append((data.get("label") or "").strip()[:100])
    if "is_visible" in data:
        fields.append("is_visible = ?"); values.append(1 if data["is_visible"] else 0)
    if "date_format" in data:
        fmt = (data.get("date_format") or "").strip()
        ok, err = patch_rules.validate_date_format(fmt)
        if not ok:
            abort(400, err)
        fields.append("date_format = ?"); values.append(fmt)
    if not fields:
        abort(400, "바꿀 내용이 없습니다.")
    fields.append("updated_at = ?"); values.append(ts()); values.append(sid)
    db.execute("UPDATE patch_sites SET %s WHERE id = ?" % ", ".join(fields), values)
    audit(db, current_user()["id"], "patch.site_updated", "patch_site", sid,
          ",".join(sorted(data)))
    db.commit()
    return jsonify(ok=True)


@api.patch("/products/<int:pid>")
@admin_required
def update_product(pid):
    db, data = get_db(), _body()
    _row_or_404(db, "patch_products", pid, "제품 라인")
    fields, values = [], []
    if "label" in data:
        fields.append("label = ?"); values.append((data.get("label") or "").strip()[:100])
    if "is_visible" in data:
        fields.append("is_visible = ?"); values.append(1 if data["is_visible"] else 0)
    if "sort_order" in data:
        fields.append("sort_order = ?"); values.append(int(data.get("sort_order") or 0))
    if "version_rule_id" in data:
        rid = data.get("version_rule_id")
        if rid:
            _row_or_404(db, "patch_version_rules", int(rid), "버전 규칙")
        fields.append("version_rule_id = ?"); values.append(int(rid) if rid else None)
    if not fields:
        abort(400, "바꿀 내용이 없습니다.")
    fields.append("updated_at = ?"); values.append(ts()); values.append(pid)
    db.execute("UPDATE patch_products SET %s WHERE id = ?" % ", ".join(fields), values)
    db.commit()
    return jsonify(ok=True)


# ---------------------------------------------------------------------------
# 모듈
# ---------------------------------------------------------------------------
@api.get("/modules")
@admin_required
def list_modules():
    db = get_db()
    pid = request.args.get("product_id", type=int)
    _row_or_404(db, "patch_products", pid, "제품 라인")
    rows = db.execute(
        "SELECT m.*, r.name AS rule_name,"
        "  (SELECT COUNT(*) FROM patch_files f WHERE f.module_id = m.id) AS file_count"
        "  FROM patch_modules m LEFT JOIN patch_version_rules r ON r.id = m.version_rule_id"
        " WHERE m.product_id = ? ORDER BY m.sort_order, m.name", (pid,)).fetchall()
    return jsonify(ok=True, modules=[{k: r[k] for k in r.keys()} for r in rows])


def _clean_module_name(name):
    name = (name or "").strip()
    if not name:
        abort(400, "모듈 이름을 입력해 주세요.")
    if len(name) > 100:
        abort(400, "모듈 이름이 너무 깁니다.")
    # 파일 이름 앞부분과 글자 그대로 같아야 하므로 경로 글자는 들어갈 수 없다
    if not patch_rules.safe_segment(name) or "_" == name[-1]:
        abort(400, "모듈 이름에 쓸 수 없는 글자가 있습니다: %s" % name)
    return name


@api.post("/modules")
@admin_required
def add_module():
    db, data = get_db(), _body()
    pid = int(data.get("product_id") or 0)
    _row_or_404(db, "patch_products", pid, "제품 라인")
    name = _clean_module_name(data.get("name"))
    if db.execute("SELECT 1 FROM patch_modules WHERE product_id = ? AND name = ?",
                  (pid, name)).fetchone():
        abort(400, "이미 등록된 모듈입니다: %s" % name)
    rule_id = data.get("version_rule_id")
    if rule_id:
        _row_or_404(db, "patch_version_rules", int(rule_id), "버전 규칙")
    now = ts()
    cur = db.execute(
        "INSERT INTO patch_modules (product_id, name, label, version_rule_id,"
        " created_at, updated_at) VALUES (?,?,?,?,?,?)",
        (pid, name, (data.get("label") or "").strip()[:100],
         int(rule_id) if rule_id else None, now, now))
    audit(db, current_user()["id"], "patch.module_added", "patch_module",
          cur.lastrowid, name)
    db.commit()
    return jsonify(ok=True, id=cur.lastrowid), 201


@api.patch("/modules/<int:mid>")
@admin_required
def update_module(mid):
    db, data = get_db(), _body()
    row = _row_or_404(db, "patch_modules", mid, "모듈")
    fields, values = [], []
    if "name" in data:
        name = _clean_module_name(data.get("name"))
        other = db.execute(
            "SELECT 1 FROM patch_modules WHERE product_id = ? AND name = ? AND id != ?",
            (row["product_id"], name, mid)).fetchone()
        if other:
            abort(400, "이미 등록된 모듈입니다: %s" % name)
        fields.append("name = ?"); values.append(name)
    if "label" in data:
        fields.append("label = ?"); values.append((data.get("label") or "").strip()[:100])
    if "is_active" in data:
        # 스캐너가 이 모듈로 매칭할지. 화면에 없고 API 로만 끈다.
        fields.append("is_active = ?"); values.append(1 if data["is_active"] else 0)
    if "is_visible" in data:
        # 사용자 화면에 보일지. 매칭은 계속하므로 다시 켜면 즉시 돌아온다.
        fields.append("is_visible = ?"); values.append(1 if data["is_visible"] else 0)
        audit(db, current_user()["id"],
              "patch.module_shown" if data["is_visible"] else "patch.module_hidden",
              "patch_module", mid, row["name"])
    if "sort_order" in data:
        fields.append("sort_order = ?"); values.append(int(data.get("sort_order") or 0))
    if "version_rule_id" in data:
        rid = data.get("version_rule_id")
        if rid:
            _row_or_404(db, "patch_version_rules", int(rid), "버전 규칙")
        fields.append("version_rule_id = ?"); values.append(int(rid) if rid else None)
    if not fields:
        abort(400, "바꿀 내용이 없습니다.")
    fields.append("updated_at = ?"); values.append(ts()); values.append(mid)
    db.execute("UPDATE patch_modules SET %s WHERE id = ?" % ", ".join(fields), values)
    db.commit()
    return jsonify(ok=True)


@api.delete("/modules/<int:mid>")
@admin_required
def delete_module(mid):
    db = get_db()
    row = _row_or_404(db, "patch_modules", mid, "모듈")
    n = db.execute("SELECT COUNT(*) c FROM patch_files WHERE module_id = ?",
                   (mid,)).fetchone()["c"]
    db.execute("DELETE FROM patch_modules WHERE id = ?", (mid,))
    audit(db, current_user()["id"], "patch.module_deleted", "patch_module", mid, row["name"])
    db.commit()
    return jsonify(ok=True, deleted=mid, files_unmatched=n,
                   note="파일 %d건이 매칭 안 된 목록으로 돌아갑니다. 파일은 그대로입니다." % n)


@api.post("/modules/copy")
@admin_required
def copy_modules():
    """다른 제품 라인의 모듈을 통째로 가져온다. 이미 있는 이름은 건너뛴다."""
    db, data = get_db(), _body()
    src = _row_or_404(db, "patch_products", int(data.get("from_product_id") or 0), "원본 제품 라인")
    dst = _row_or_404(db, "patch_products", int(data.get("to_product_id") or 0), "대상 제품 라인")
    if src["id"] == dst["id"]:
        abort(400, "같은 제품 라인입니다.")
    rows = db.execute("SELECT * FROM patch_modules WHERE product_id = ?",
                      (src["id"],)).fetchall()
    now, made = ts(), 0
    for r in rows:
        if db.execute("SELECT 1 FROM patch_modules WHERE product_id = ? AND name = ?",
                      (dst["id"], r["name"])).fetchone():
            continue
        db.execute(
            "INSERT INTO patch_modules (product_id, name, label, version_rule_id,"
            " sort_order, is_active, is_visible, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (dst["id"], r["name"], r["label"], r["version_rule_id"], r["sort_order"],
             r["is_active"], r["is_visible"], now, now))
        made += 1
    audit(db, current_user()["id"], "patch.modules_copied", "patch_product", dst["id"],
          "from=%d n=%d" % (src["id"], made))
    db.commit()
    return jsonify(ok=True, copied=made, skipped=len(rows) - made)


# ---------------------------------------------------------------------------
# 파일 공개 여부
#
# 사이트 → 제품 라인 → 모듈 까지는 묶음 단위로 끄고, 여기서는 tar 한 건을
# 끈다. 잘못 올라간 빌드, 특정 고객사에만 나가야 하는 패치처럼 "묶음은
# 맞는데 이 파일만 아니다" 가 실제로 생긴다.
#
# 끄는 것은 색인의 칸 하나일 뿐이고 DAS 의 파일은 건드리지 않는다. 스캔이
# 다시 돌아도 이 칸은 그대로다(patch_scan 의 UPDATE 문에 들어 있지 않다).
# ---------------------------------------------------------------------------
@api.get("/files")
@admin_required
def list_files():
    """
    한 제품 라인의 파일. 사용자 화면과 달리 숨긴 것, 매칭 안 된 것, 사라진
    것까지 모두 보여 준다. 끈 것을 다시 찾을 수 있어야 하므로 거르지 않는다.
    날짜 목록을 함께 주어 화면이 한 번만 부르게 한다.
    """
    db = get_db()
    pid = request.args.get("product_id", type=int)
    product = _row_or_404(db, "patch_products", pid, "제품 라인")

    dates = [{"date_dir": r["date_dir"], "date_at": r["date_at"],
              "count": r["n"], "hidden": r["hidden"]}
             for r in db.execute(
                 "SELECT date_dir, MAX(date_at) AS date_at, COUNT(*) AS n,"
                 "       SUM(CASE WHEN is_visible = 0 THEN 1 ELSE 0 END) AS hidden"
                 "  FROM patch_files WHERE product_id = ?"
                 " GROUP BY date_dir"
                 " ORDER BY (date_at IS NULL), date_at DESC, date_dir DESC",
                 (pid,)).fetchall()]

    date_dir = (request.args.get("date") or "").strip()
    if not date_dir and dates:
        date_dir = dates[0]["date_dir"]

    files = []
    if date_dir:
        files = [{
            "id": r["id"],
            "filename": r["filename"],
            "module": r["module_name"],
            "module_visible": None if r["module_name"] is None else bool(r["module_visible"]),
            "version": r["version"],
            "size": r["size"],
            "is_visible": bool(r["is_visible"]),
            "is_missing": bool(r["is_missing"]),
            "content_changed": bool(r["content_changed"]),
        } for r in db.execute(
            "SELECT f.*, m.name AS module_name, m.is_visible AS module_visible"
            "  FROM patch_files f LEFT JOIN patch_modules m ON m.id = f.module_id"
            " WHERE f.product_id = ? AND f.date_dir = ?"
            " ORDER BY (f.module_id IS NULL), m.sort_order, m.name,"
            "          f.version_sort DESC, f.filename",
            (pid, date_dir)).fetchall()]

    return jsonify(ok=True, product={"id": product["id"], "name": product["name"]},
                   dates=dates, date=date_dir, files=files)


@api.patch("/files/<int:fid>")
@admin_required
def update_file(fid):
    db, data = get_db(), _body()
    row = _row_or_404(db, "patch_files", fid, "파일")
    if "is_visible" not in data:
        abort(400, "바꿀 내용이 없습니다.")
    on = 1 if data["is_visible"] else 0
    db.execute("UPDATE patch_files SET is_visible = ? WHERE id = ?", (on, fid))
    audit(db, current_user()["id"],
          "patch.file_shown" if on else "patch.file_hidden",
          "patch_file", fid, "%s/%s" % (row["date_dir"], row["filename"]))
    db.commit()
    return jsonify(ok=True)


@api.post("/files/visibility")
@admin_required
def set_files_visibility():
    """
    여러 건을 한 번에. 날짜 폴더 하나를 통째로 끄는 것이 가장 흔하다.
    (잘못 올라간 배포 하루를 걷어낼 때)

        {"product_id": 3, "date": "260923", "is_visible": false}
        {"ids": [11, 12], "is_visible": true}

    날짜를 지정하면 그 폴더의 파일 전부에 적용한다. 날짜 폴더 자체를 따로
    기록하지 않고 파일에 거는 이유는, 그래야 나중에 들어온 파일이 조용히
    숨겨지지 않고 보이는 상태로 들어오기 때문이다. 하루를 다시 걷어내야
    하면 한 번 더 누르면 된다. 모르는 사이에 안 보이는 것보다 낫다.
    """
    db, data = get_db(), _body()
    if "is_visible" not in data:
        abort(400, "is_visible 값이 필요합니다.")
    on = 1 if data["is_visible"] else 0

    ids = data.get("ids")
    if ids:
        try:
            ids = [int(i) for i in ids]
        except (TypeError, ValueError):
            abort(400, "파일 id 가 잘못됐습니다.")
        if len(ids) > 2000:
            abort(400, "한 번에 2000건까지만 됩니다.")
        marks = ",".join("?" * len(ids))
        n = db.execute("UPDATE patch_files SET is_visible = ? WHERE id IN (%s)" % marks,
                       [on] + ids).rowcount
        target, label = "patch_file", "ids=%d건" % len(ids)
    else:
        pid = int(data.get("product_id") or 0)
        product = _row_or_404(db, "patch_products", pid, "제품 라인")
        date_dir = (data.get("date") or "").strip()
        sql = "UPDATE patch_files SET is_visible = ? WHERE product_id = ?"
        params = [on, pid]
        if date_dir:
            sql += " AND date_dir = ?"
            params.append(date_dir)
        n = db.execute(sql, params).rowcount
        target = "patch_product"
        label = "%s %s" % (product["name"], date_dir or "(전체 날짜)")

    audit(db, current_user()["id"],
          "patch.files_shown" if on else "patch.files_hidden",
          target, data.get("product_id") or "", "%s n=%d" % (label, n))
    db.commit()
    return jsonify(ok=True, changed=n)


# ---------------------------------------------------------------------------
# 버전 규칙
# ---------------------------------------------------------------------------
@api.get("/version-rules")
@admin_required
def list_rules():
    db = get_db()
    rows = db.execute("SELECT * FROM patch_version_rules"
                      " ORDER BY is_builtin DESC, name").fetchall()
    return jsonify(ok=True, rules=[{k: r[k] for k in r.keys()} for r in rows],
                   sort_kinds=list(patch_rules.SORT_KINDS))


@api.post("/version-rules")
@admin_required
def add_rule():
    db, data = get_db(), _body()
    name = (data.get("name") or "").strip()
    if not name or len(name) > 60:
        abort(400, "규칙 이름을 입력해 주세요. (최대 60자)")
    pattern = (data.get("pattern") or "").strip()
    sample = (data.get("sample") or "").strip()
    # 샘플 통과를 강제한다. 잘못된 정규식 하나가 제품 라인 전체를 빈 목록으로
    # 만드는 일을 막는 유일한 장치다.
    if not sample:
        abort(400, "샘플 버전을 입력해 주세요. 저장 전에 실제로 잡히는지 확인합니다.")
    ok, err = patch_rules.validate_version_pattern(pattern, sample)
    if not ok:
        abort(400, err)
    kind = (data.get("sort_kind") or "numeric").strip()
    if kind not in patch_rules.SORT_KINDS:
        abort(400, "정렬 방식이 올바르지 않습니다.")
    if db.execute("SELECT 1 FROM patch_version_rules WHERE name = ?", (name,)).fetchone():
        abort(400, "이미 있는 규칙 이름입니다.")
    now = ts()
    cur = db.execute(
        "INSERT INTO patch_version_rules (name, pattern, sort_kind, sample, is_builtin,"
        " created_at, updated_at) VALUES (?,?,?,?,0,?,?)",
        (name, pattern, kind, sample, now, now))
    audit(db, current_user()["id"], "patch.rule_added", "patch_rule", cur.lastrowid, name)
    db.commit()
    return jsonify(ok=True, id=cur.lastrowid), 201


@api.patch("/version-rules/<int:rid>")
@admin_required
def update_rule(rid):
    db, data = get_db(), _body()
    row = _row_or_404(db, "patch_version_rules", rid, "버전 규칙")
    pattern = (data.get("pattern") or row["pattern"]).strip()
    sample = (data.get("sample") or row["sample"]).strip()
    ok, err = patch_rules.validate_version_pattern(pattern, sample)
    if not ok:
        abort(400, err)
    kind = (data.get("sort_kind") or row["sort_kind"]).strip()
    if kind not in patch_rules.SORT_KINDS:
        abort(400, "정렬 방식이 올바르지 않습니다.")
    db.execute(
        "UPDATE patch_version_rules SET pattern = ?, sample = ?, sort_kind = ?,"
        " updated_at = ? WHERE id = ?",
        (pattern, sample, kind, ts(), rid))
    audit(db, current_user()["id"], "patch.rule_updated", "patch_rule", rid, row["name"])
    db.commit()
    return jsonify(ok=True)


@api.delete("/version-rules/<int:rid>")
@admin_required
def delete_rule(rid):
    db = get_db()
    row = _row_or_404(db, "patch_version_rules", rid, "버전 규칙")
    used = db.execute(
        "SELECT (SELECT COUNT(*) FROM patch_products WHERE version_rule_id = ?)"
        "     + (SELECT COUNT(*) FROM patch_modules WHERE version_rule_id = ?) AS c",
        (rid, rid)).fetchone()["c"]
    if used:
        abort(400, "이 규칙을 쓰는 제품 라인/모듈이 %d곳 있습니다. 먼저 바꿔 주세요." % used)
    db.execute("DELETE FROM patch_version_rules WHERE id = ?", (rid,))
    audit(db, current_user()["id"], "patch.rule_deleted", "patch_rule", rid, row["name"])
    db.commit()
    return jsonify(ok=True, deleted=rid)


# ---------------------------------------------------------------------------
# 파일명 / 날짜 형식 테스트
#
# 사소해 보이지만 실제로 가장 자주 쓰일 자리다. 모듈을 등록했는데 목록이
# 비었을 때 이유를 찾는 유일한 도구다.
# ---------------------------------------------------------------------------
@api.post("/parse-test")
@admin_required
def parse_test():
    db, data = get_db(), _body()
    out = {"ok": True}

    filename = (data.get("filename") or "").strip()
    if filename:
        pid = int(data.get("product_id") or 0)
        product = _row_or_404(db, "patch_products", pid, "제품 라인")
        modules = patch_scan.modules_for(db, product)
        if not patch_rules.is_patch_file(filename):
            out["file"] = {"matched": False,
                           "why": "*.tar 가 아니거나 임시 파일 이름입니다."}
        else:
            hit = patch_rules.match_file(filename, modules)
            if hit:
                out["file"] = {"matched": True, "module": hit["module"]["name"],
                               "version": hit["version"], "suffix": hit["suffix"],
                               "version_sort": hit["version_sort"]}
            else:
                starts = [m["name"] for m in modules
                          if filename.startswith(m["name"] + "_")]
                why = ("이름이 맞는 모듈이 없습니다. 등록된 모듈 %d개 중 앞부분이"
                       " 같은 것이 없습니다." % len(modules)) if not starts else (
                       "모듈 %s 은(는) 맞지만 뒤의 '%s' 가 버전 규칙에 맞지 않습니다."
                       % (", ".join(starts), filename))
                out["file"] = {"matched": False, "why": why,
                               "candidate": patch_rules.candidate_module_name(filename)}

    date_dir = (data.get("date_dir") or "").strip()
    fmt = (data.get("date_format") or "").strip()
    if date_dir or fmt:
        ok, err = patch_rules.validate_date_format(fmt)
        if not ok:
            out["date"] = {"matched": False, "why": err}
        else:
            parsed = patch_rules.parse_date_dir(date_dir, fmt)
            out["date"] = {"matched": parsed is not None, "parsed": parsed,
                           "why": "" if parsed else
                           "'%s' 이(가) 형식 %s 에 맞지 않습니다." % (date_dir, fmt)}
    return jsonify(**out)


# ---------------------------------------------------------------------------
# 스캔
# ---------------------------------------------------------------------------
@api.post("/scan")
@admin_required
def run_scan():
    db, data = get_db(), _body()
    root_id = data.get("root_id")
    if root_id:
        _row_or_404(db, "patch_roots", int(root_id), "와칭 루트")
    res = patch_scan.scan(db, int(root_id) if root_id else None, "manual")
    if res.get("busy"):
        return jsonify(ok=False, busy=True, error=res["error"]), 409
    audit(db, current_user()["id"], "patch.scan", "patch_root", root_id or "all",
          "roots=%d" % len(res["results"]))
    db.commit()
    return jsonify(ok=True, results=res["results"])


@api.get("/scans")
@admin_required
def list_scans():
    db = get_db()
    rows = db.execute(
        "SELECT sc.*, r.path AS root_path, r.label AS root_label"
        "  FROM patch_scans sc LEFT JOIN patch_roots r ON r.id = sc.root_id"
        " ORDER BY sc.id DESC LIMIT 30").fetchall()
    return jsonify(ok=True, scans=[{k: r[k] for k in r.keys()} for r in rows])


# ---------------------------------------------------------------------------
# 매칭 안 된 파일
# ---------------------------------------------------------------------------
@api.get("/unmatched")
@admin_required
def unmatched():
    db = get_db()
    sql = ["SELECT f.id, f.filename, f.date_dir, f.product_id, f.size,",
           "       p.name AS product_name, s.name AS site_name",
           "  FROM patch_files f",
           "  JOIN patch_products p ON p.id = f.product_id",
           "  JOIN patch_sites s ON s.id = p.site_id",
           " WHERE f.module_id IS NULL AND f.is_missing = 0"]
    params = []
    pid = request.args.get("product_id", type=int)
    if pid:
        sql.append(" AND f.product_id = ?")
        params.append(pid)
    sql.append(" ORDER BY s.name, p.name, f.date_dir DESC, f.filename LIMIT 200")
    rows = db.execute("".join(sql), params).fetchall()
    out = []
    for r in rows:
        d = {k: r[k] for k in r.keys()}
        d["candidate"] = patch_rules.candidate_module_name(r["filename"])
        out.append(d)
    return jsonify(ok=True, files=out)


@api.post("/unmatched/register")
@admin_required
def register_unmatched():
    """
    고른 이름들을 모듈로 한 번에 등록하고 그 루트를 다시 스캔한다.
    스캔까지 함께 도는 것이 중요하다. 등록만 하고 끝내면 "등록했는데 목록이
    그대로" 가 되어 사람이 원인을 찾게 된다.
    """
    db, data = get_db(), _body()
    pid = int(data.get("product_id") or 0)
    product = _row_or_404(db, "patch_products", pid, "제품 라인")
    names = [(n or "").strip() for n in (data.get("names") or []) if (n or "").strip()]
    if not names:
        abort(400, "등록할 모듈 이름을 하나 이상 고르세요.")

    now, made, skipped = ts(), [], []
    for raw in names:
        name = _clean_module_name(raw)
        if db.execute("SELECT 1 FROM patch_modules WHERE product_id = ? AND name = ?",
                      (pid, name)).fetchone():
            skipped.append(name)
            continue
        db.execute(
            "INSERT INTO patch_modules (product_id, name, created_at, updated_at)"
            " VALUES (?,?,?,?)", (pid, name, now, now))
        made.append(name)
    audit(db, current_user()["id"], "patch.modules_registered", "patch_product", pid,
          ",".join(made)[:200])
    db.commit()

    site = db.execute("SELECT root_id FROM patch_sites WHERE id = ?",
                      (product["site_id"],)).fetchone()
    res = patch_scan.scan(db, site["root_id"], "manual")
    return jsonify(ok=True, registered=made, skipped=skipped,
                   scan=res["results"][0] if res["results"] else None)
