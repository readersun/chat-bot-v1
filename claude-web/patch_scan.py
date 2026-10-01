#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
patch_scan
==========

와칭 루트를 걸으며 DB 색인을 맞춘다.

    /{루트}/{사이트}/{제품 라인}/{날짜}/{모듈}_{버전}*.tar
       ↑      ↑         ↑          ↑        ↑
     등록   스캔이    스캔이      스캔이   등록된 모듈만
     (사람) 찾는다    찾는다      찾는다

원칙
----
**파일시스템이 진짜고 DB 는 색인이다.** 색인을 통째로 지워도 스캔 한 번으로
전부 돌아와야 한다. 그래서 파일에 없는 정보를 DB 에 만들어 넣지 않는다.

**읽기만 한다.** 이 파일에는 unlink / rename / mkdir / open(…, 'w') 가 하나도
없다. 고객사에 나간 패치 원본을 웹 앱의 버그가 고칠 수 없게 한다.

조심한 것 넷
------------
1. 복사 중인 파일을 받지 않는다. rsync 가 3GB 를 올리는 중에 스캔이 돌면
   중도까지 만 파일이 최신 버전으로 올라가고 현장은 그걸 받아 간다.
   나이 제한 + 크기 안정 확인 + 이름 제외, 셋을 겹친다.
2. 마운트가 빠진 루트를 "전부 사라졌다" 로 적지 않는다. 루트가 없거나 비면
   그 루트만 오류로 적고 색인은 그대로 둔다.
3. 한 루트가 죽어도 나머지는 돈다. 루트마다 try 로 감싼다.
4. 사라진 파일은 행을 지우지 않는다. is_missing 으로 표시만 한다. 어떤 버전이
   있었다는 사실 자체가 기록이다.

성능
----
루트 전체를 걷되 파일은 stat 만 하고, (size, mtime_ns) 가 같으면 DB 를 건드리지
않는다. 날짜 폴더가 몇 년 치 쌓여도 stat 비용뿐이다. 이보다 더 줄여야 할 만큼
커지면 그때 날짜 폴더 mtime 으로 증분을 넣는다. 지금 넣으면 캐시가 어긋났을 때
"목록에 안 뜬다" 는 진단하기 어려운 버그가 된다.
"""

import hashlib
import os
import threading
import time

import patch_rules
from db import ts

# 복사가 끝나지 않은 파일을 거르는 두 장치.
HOLD_SECONDS = 60          # 이보다 최근에 바뀐 파일은 다음 스캔으로 미룬다
SIZE_SETTLE_SECONDS = 1.0  # 두 번 stat 해서 크기가 변하면 미룬다

# 한 번에 하나만 돈다. gunicorn 워커가 1개라 프로세스 내 락으로 충분하다.
# (app.ConcurrencyLimiter 와 같은 근거다)
_SCAN_LOCK = threading.Lock()

HASH_CHUNK = 1024 * 1024


def _empty_counts():
    return {"added": 0, "updated": 0, "missing": 0, "held": 0,
            "unmatched": 0, "off_rule": 0, "new_sites": 0, "new_products": 0}


# ---------------------------------------------------------------------------
# 파일 하나 보기
# ---------------------------------------------------------------------------
def _stat_now(path):
    """(size, mtime_ns, 나이초) 또는 None."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    return st.st_size, st.st_mtime_ns, time.time() - st.st_mtime


def _is_settled(path, size, mtime_ns):
    """
    잠깐 뒤에 다시 재서 그대로인가. 아직 커지고 있으면 False.

    나이 제한을 이미 통과한 파일에만 쓴다. NFS 는 속성을 캐시해서 mtime 이
    늦게 올라오는 경우가 있어 나이 제한만으로는 모자란다. 대신 모든 파일을
    두 번 stat 하면 스캔이 두 배로 느려지므로, 새로 들어오거나 바뀐 파일에만
    건다. 이미 색인돼 있고 그대로인 파일은 이 길로 오지 않는다.
    """
    time.sleep(SIZE_SETTLE_SECONDS)
    st = _stat_now(path)
    return st is not None and st[0] == size and st[1] == mtime_ns


def _sha256(path):
    h = hashlib.sha256()
    try:
        with open(path, "rb") as fp:
            for chunk in iter(lambda: fp.read(HASH_CHUNK), b""):
                h.update(chunk)
    except OSError:
        return ""
    return h.hexdigest()


def _listdir(path):
    try:
        return sorted(os.listdir(path))
    except OSError:
        return None


# ---------------------------------------------------------------------------
# 발견한 것 등록
# ---------------------------------------------------------------------------
def _upsert_site(db, root, name, counts):
    row = db.execute("SELECT * FROM patch_sites WHERE root_id = ? AND name = ?",
                     (root["id"], name)).fetchone()
    if row:
        return row
    now = ts()
    # 새로 발견한 사이트는 숨김으로 들어온다. 고객사 폴더가 하나 생겼다고
    # 그 순간 전원에게 보이면 안 된다. 관리자가 켜야 목록에 나온다.
    db.execute(
        "INSERT INTO patch_sites (root_id, name, date_format, is_visible,"
        " discovered_at, updated_at) VALUES (?,?,?,0,?,?)",
        (root["id"], name, root["date_format"], now, now))
    counts["new_sites"] += 1
    return db.execute("SELECT * FROM patch_sites WHERE root_id = ? AND name = ?",
                      (root["id"], name)).fetchone()


def _upsert_product(db, site, name, counts):
    row = db.execute("SELECT * FROM patch_products WHERE site_id = ? AND name = ?",
                     (site["id"], name)).fetchone()
    if row:
        return row
    now = ts()
    db.execute(
        "INSERT INTO patch_products (site_id, name, is_visible, discovered_at, updated_at)"
        " VALUES (?,?,0,?,?)", (site["id"], name, now, now))
    counts["new_products"] += 1
    return db.execute("SELECT * FROM patch_products WHERE site_id = ? AND name = ?",
                      (site["id"], name)).fetchone()


def modules_for(db, product):
    """
    그 제품 라인의 모듈 목록. 규칙은 모듈 -> 제품 라인 -> 내장 기본값 순으로
    물려받는다. match_file 이 바로 쓸 수 있는 모양으로 만들어 돌려준다.
    """
    default = db.execute(
        "SELECT pattern, sort_kind FROM patch_version_rules WHERE id = ?",
        (product["version_rule_id"],)).fetchone() if product["version_rule_id"] else None
    if default is None:
        default = db.execute(
            "SELECT pattern, sort_kind FROM patch_version_rules WHERE name = ?",
            (patch_rules.DEFAULT_VERSION_RULE,)).fetchone()

    rows = db.execute(
        "SELECT m.id, m.name, m.version_rule_id, r.pattern, r.sort_kind"
        "  FROM patch_modules m"
        "  LEFT JOIN patch_version_rules r ON r.id = m.version_rule_id"
        " WHERE m.product_id = ? AND m.is_active = 1", (product["id"],)).fetchall()

    out = []
    for r in rows:
        pattern = r["pattern"] or (default["pattern"] if default else "")
        kind = r["sort_kind"] or (default["sort_kind"] if default else "numeric")
        out.append({"id": r["id"], "name": r["name"],
                    "pattern": pattern, "sort_kind": kind})
    return out


# ---------------------------------------------------------------------------
# 루트 하나 걷기
# ---------------------------------------------------------------------------
def _scan_product_dir(db, root, site, product, product_path, counts, seen):
    """제품 라인 폴더 안의 날짜 폴더들을 돈다."""
    modules = modules_for(db, product)
    entries = _listdir(product_path)
    if entries is None:
        return

    for date_name in entries:
        date_path = os.path.join(product_path, date_name)
        if not os.path.isdir(date_path):
            # 날짜 폴더 없이 제품 폴더에 바로 든 tar. 규칙과 다른 깊이다.
            if patch_rules.is_patch_file(date_name):
                counts["off_rule"] += 1
            continue
        if not patch_rules.safe_segment(date_name):
            continue

        date_at = patch_rules.parse_date_dir(date_name, site["date_format"])
        files = _listdir(date_path)
        if files is None:
            continue

        for fname in files:
            fpath = os.path.join(date_path, fname)
            if not patch_rules.is_patch_file(fname):
                continue
            if not os.path.isfile(fpath):
                continue

            st = _stat_now(fpath)
            if st is None:
                continue
            size, mtime_ns, age = st

            # 복사 중인 파일을 받지 않는 1차 장치: 나이 제한.
            # rsync 가 쓰고 있는 동안에는 mtime 이 계속 올라오므로 여기서 걸린다.
            # 다음 스캔에서 다시 본다. 보류는 삭제가 아니다.
            if age < HOLD_SECONDS:
                counts["held"] += 1
                continue

            # 매칭은 문자열 연산뿐이라 싸다. 빠른 길 판단보다 먼저 해 둔다.
            # 늦게 하면 "모듈을 새로 등록했는데 다시 스캔해도 안 잡힌다" 가 된다.
            # 파일은 그대로이므로 크기/시각만 보면 건너뛰어 버리기 때문이다.
            hit = patch_rules.match_file(fname, modules)
            if hit is None:
                counts["unmatched"] += 1

            rel = "/".join([site["name"], product["name"], date_name, fname])
            row = db.execute(
                "SELECT * FROM patch_files"
                " WHERE product_id = ? AND date_dir = ? AND filename = ?",
                (product["id"], date_name, fname)).fetchone()

            # 빠른 길: 파일도 그대로고 읽어 낸 결과도 그대로면 아무것도 안 한다.
            if row is not None and not row["is_missing"] \
                    and row["size"] == size and row["mtime_ns"] == mtime_ns \
                    and row["module_id"] == (hit["module"]["id"] if hit else None) \
                    and row["version"] == (hit["version"] if hit else "") \
                    and row["suffix"] == (hit["suffix"] if hit else "") \
                    and row["date_at"] == date_at and row["rel_path"] == rel:
                seen.add(row["id"])
                db.execute("UPDATE patch_files SET last_seen_at = ? WHERE id = ?",
                           (ts(), row["id"]))
                continue

            # 2차 장치: 새로 들어오거나 바뀐 파일만 한 번 더 재 본다.
            if not _is_settled(fpath, size, mtime_ns):
                counts["held"] += 1
                continue

            _record_file(db, product, row, date_name, date_at, fname, rel,
                         hit, size, mtime_ns, root["hash_enabled"], fpath, counts, seen)


def _record_file(db, product, row, date_dir, date_at, fname, rel, hit,
                 size, mtime_ns, hash_enabled, fpath, counts, seen):
    module_id = hit["module"]["id"] if hit else None
    version = hit["version"] if hit else ""
    version_sort = hit["version_sort"] if hit else ""
    suffix = hit["suffix"] if hit else ""
    now = ts()

    if row is None:
        sha = _sha256(fpath) if hash_enabled else ""
        db.execute(
            "INSERT INTO patch_files (product_id, module_id, date_dir, date_at, filename,"
            " rel_path, version, version_sort, suffix, size, mtime_ns, sha256,"
            " content_changed, is_missing, first_seen_at, last_seen_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,0,0,?,?)",
            (product["id"], module_id, date_dir, date_at, fname, rel, version,
             version_sort, suffix, size, mtime_ns, sha, now, now))
        counts["added"] += 1
        seen.add(db.execute(
            "SELECT id FROM patch_files WHERE product_id = ? AND date_dir = ? AND filename = ?",
            (product["id"], date_dir, fname)).fetchone()["id"])
        return

    seen.add(row["id"])

    # 같은 파일명인데 크기가 달라졌다 = 같은 버전 번호로 다른 바이너리가 올라왔다.
    # 조용히 바꾸지 않고 표시를 남긴다. 현장에서 가장 찾기 어려운 사고다.
    # (크기가 같고 mtime 만 바뀐 것은 같은 파일 재복사로 보고 표시하지 않는다.
    #  해시를 켜 두면 내용까지 비교해 더 정확해진다)
    changed = bool(row["content_changed"])
    sha = row["sha256"]
    if hash_enabled:
        sha = _sha256(fpath)
        if row["sha256"] and sha and sha != row["sha256"]:
            changed = True
    if row["size"] != size and not row["is_missing"]:
        changed = True

    db.execute(
        "UPDATE patch_files SET module_id = ?, date_at = ?, rel_path = ?, version = ?,"
        " version_sort = ?, suffix = ?, size = ?, mtime_ns = ?, sha256 = ?,"
        " content_changed = ?, is_missing = 0, last_seen_at = ? WHERE id = ?",
        (module_id, date_at, rel, version, version_sort, suffix, size, mtime_ns,
         sha, 1 if changed else 0, now, row["id"]))
    counts["updated"] += 1


def scan_root(db, root, trigger_kind="manual"):
    """
    루트 하나를 돈다. 돌려주는 값: counts dict (+ "error" 가 있으면 실패).
    예외를 밖으로 던지지 않는다. 한 루트가 죽어도 나머지가 돌아야 한다.
    """
    counts = _empty_counts()
    started = ts()
    cur = db.execute(
        "INSERT INTO patch_scans (root_id, trigger_kind, started_at) VALUES (?,?,?)",
        (root["id"], trigger_kind, started))
    scan_id = cur.lastrowid
    error = ""

    try:
        entries = _listdir(root["path"])
        # 마운트 점검. 경로가 없거나 하위 폴더가 하나도 없으면 시작하지 않는다.
        # 마운트가 떨어진 단 한 번으로 그 루트의 모든 파일이 사라진 것으로
        # 도장 찍히는 사고를 막는다.
        if entries is None:
            error = "루트 경로를 읽을 수 없습니다: %s" % root["path"]
        elif not any(os.path.isdir(os.path.join(root["path"], e)) for e in entries):
            error = "루트 아래에 폴더가 하나도 없습니다. 마운트를 확인해 주세요: %s" % root["path"]

        if not error:
            seen = set()
            for site_name in entries:
                site_path = os.path.join(root["path"], site_name)
                if not os.path.isdir(site_path):
                    if patch_rules.is_patch_file(site_name):
                        counts["off_rule"] += 1
                    continue
                if not patch_rules.safe_segment(site_name):
                    continue

                site = _upsert_site(db, root, site_name, counts)
                prod_entries = _listdir(site_path) or []
                for prod_name in prod_entries:
                    prod_path = os.path.join(site_path, prod_name)
                    if not os.path.isdir(prod_path):
                        if patch_rules.is_patch_file(prod_name):
                            counts["off_rule"] += 1
                        continue
                    if not patch_rules.safe_segment(prod_name):
                        continue
                    product = _upsert_product(db, site, prod_name, counts)
                    _scan_product_dir(db, root, site, product, prod_path, counts, seen)

            counts["missing"] = _mark_missing(db, root, seen)
            db.execute("UPDATE patch_roots SET last_scanned_at = ?, last_error = ''"
                       " WHERE id = ?", (ts(), root["id"]))
    except Exception as exc:                      # pragma: no cover
        error = "%s: %s" % (type(exc).__name__, exc)

    if error:
        db.execute("UPDATE patch_roots SET last_scanned_at = ?, last_error = ?"
                   " WHERE id = ?", (ts(), error[:300], root["id"]))

    db.execute(
        "UPDATE patch_scans SET finished_at = ?, added = ?, updated = ?, missing = ?,"
        " held = ?, unmatched = ?, off_rule = ?, new_sites = ?, new_products = ?,"
        " error = ? WHERE id = ?",
        (ts(), counts["added"], counts["updated"], counts["missing"], counts["held"],
         counts["unmatched"], counts["off_rule"], counts["new_sites"],
         counts["new_products"], error[:300], scan_id))
    db.commit()

    out = dict(counts)
    out["scan_id"] = scan_id
    out["root_id"] = root["id"]
    out["error"] = error
    return out


def _mark_missing(db, root, seen):
    """이번에 못 본 파일에 표시를 단다. 행을 지우지는 않는다."""
    rows = db.execute(
        "SELECT f.id FROM patch_files f"
        "  JOIN patch_products p ON p.id = f.product_id"
        "  JOIN patch_sites s ON s.id = p.site_id"
        " WHERE s.root_id = ? AND f.is_missing = 0", (root["id"],)).fetchall()
    gone = [r["id"] for r in rows if r["id"] not in seen]
    if gone:
        db.executemany("UPDATE patch_files SET is_missing = 1 WHERE id = ?",
                       [(i,) for i in gone])
    return len(gone)


# ---------------------------------------------------------------------------
# 바깥에서 부르는 것
# ---------------------------------------------------------------------------
def scan(db, root_id=None, trigger_kind="manual"):
    """
    루트 하나 또는 전부를 돈다. 이미 돌고 있으면 기다리지 않고 바로 돌려준다.
    (사용자가 '지금 검사' 를 두 번 눌러도 두 번 돌지 않는다)
    """
    if not _SCAN_LOCK.acquire(blocking=False):
        return {"ok": False, "busy": True, "results": [],
                "error": "이미 검사가 돌고 있습니다. 잠시 뒤 다시 눌러 주세요."}
    try:
        if root_id is None:
            roots = db.execute(
                "SELECT * FROM patch_roots WHERE scan_enabled = 1"
                " ORDER BY COALESCE(last_scanned_at, '') ASC, id ASC").fetchall()
        else:
            roots = db.execute("SELECT * FROM patch_roots WHERE id = ?",
                               (root_id,)).fetchall()
        results = [scan_root(db, r, trigger_kind) for r in roots]
        return {"ok": True, "busy": False, "results": results, "error": ""}
    finally:
        _SCAN_LOCK.release()


def due_roots(db, now=None):
    """주기가 돌아온 루트. last_scanned_at 이 가장 오래된 것부터."""
    rows = db.execute(
        "SELECT * FROM patch_roots WHERE scan_enabled = 1"
        " ORDER BY COALESCE(last_scanned_at, '') ASC, id ASC").fetchall()
    now = now or time.time()
    out = []
    for r in rows:
        last = r["last_scanned_at"]
        if not last:
            out.append(r)
            continue
        try:
            age = now - time.mktime(time.strptime(last, "%Y-%m-%d %H:%M:%S"))
        except (ValueError, OverflowError):
            out.append(r)
            continue
        if age >= max(60, int(r["scan_interval_s"] or 600)):
            out.append(r)
    return out
