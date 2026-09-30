#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
storage
=======

관리자 Storage 화면에 쓰는 저장공간 계산.

측정 기준
---------
디스크 용량은 컨테이너의 root filesystem 이 아니라 **실제 데이터가 저장되는**
파일시스템을 봐야 의미가 있다. Docker 배포에서 /app 은 읽기전용 bind mount 이고
DB / 업로드 / 메모 첨부는 전부 host 의 볼륨(/var/lib/claude-web)에 있다.
그래서 config.STORAGE_MONITOR_PATH(기본값 = DB 가 있는 디렉터리)를 기준으로
shutil.disk_usage() 를 호출한다. df / du 같은 셸 명령을 파싱하지 않는다.

성능
----
디렉터리 용량은 파일을 전부 순회해야 나온다. 파일이 수만 개가 되면 매 요청마다
스캔하는 비용이 커지므로

  - **데이터 루트를 한 번만 순회**해서 하위 항목별 크기를 동시에 구한다.
    (uploads / notes / backups 를 따로 스캔하면 같은 파일을 여러 번 읽는다)
  - 결과를 config.STORAGE_CACHE_SECONDS 동안 캐시한다.
  - 화면의 [새로고침] 은 refresh=True 로 캐시를 무시한다.

Redis 같은 외부 저장소는 쓰지 않는다. gunicorn 워커가 1개이므로 프로세스 안의
dict + Lock 으로 충분하다. (deploy/gunicorn.conf.py 의 workers = 1)

심볼릭 링크
-----------
순회 중 심볼릭 링크는 **따라가지 않는다.** 따라가면 데이터 디렉터리 안의 링크
하나로 /usr 전체를 세거나 순환에 빠질 수 있다. 링크 자체의 크기도 세지 않는다.
"""

import os
import shutil
import threading
import time

import config
import settings_store

_CACHE = {"at": 0.0, "data": None}
_CACHE_LOCK = threading.Lock()

# 사용률 경고 기준 (%). 27번 요구사항.
WARN_PERCENT = 70
DANGER_PERCENT = 85


# ---------------------------------------------------------------------------
# 디렉터리 순회
# ---------------------------------------------------------------------------
def dir_usage(path):
    """
    (bytes, files) 를 돌려준다. 심볼릭 링크는 따라가지 않는다.
    읽을 수 없는 디렉터리는 건너뛴다. (권한 문제로 화면 전체가 죽지 않게)
    """
    total = 0
    count = 0
    stack = [path]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as it:
                for entry in it:
                    try:
                        # follow_symlinks=False 이므로 링크는 dir 도 file 도 아니다.
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(entry.path)
                        elif entry.is_file(follow_symlinks=False):
                            total += entry.stat(follow_symlinks=False).st_size
                            count += 1
                    except OSError:
                        continue
        except OSError:
            continue
    return total, count


def scan_root(root):
    """
    root 바로 아래의 항목별 사용량을 **한 번의 순회**로 구한다.
    반환: {"children": {이름: (bytes, files)}, "total": (bytes, files)}
    """
    children = {}
    total_bytes = 0
    total_files = 0
    try:
        with os.scandir(root) as it:
            entries = list(it)
    except OSError:
        return {"children": children, "total": (0, 0)}

    for entry in entries:
        try:
            if entry.is_dir(follow_symlinks=False):
                size, files = dir_usage(entry.path)
            elif entry.is_file(follow_symlinks=False):
                size, files = entry.stat(follow_symlinks=False).st_size, 1
            else:
                continue  # 심볼릭 링크 등은 세지 않는다
        except OSError:
            continue
        children[entry.name] = (size, files)
        total_bytes += size
        total_files += files
    return {"children": children, "total": (total_bytes, total_files)}


def usage_of(target, root, scanned):
    """
    target 의 사용량을 돌려준다. target 이 root 바로 아래면 이미 구한 값을 쓰고,
    (데이터 루트 밖에 있는 경로처럼) 아니면 그때만 따로 순회한다.
    """
    if not target:
        return 0, 0
    target = os.path.abspath(target)
    if os.path.dirname(target) == root and os.path.basename(target) in scanned["children"]:
        return scanned["children"][os.path.basename(target)]
    if not os.path.isdir(target):
        return 0, 0
    return dir_usage(target)


# ---------------------------------------------------------------------------
# 디스크
# ---------------------------------------------------------------------------
def disk_usage(path):
    """
    파일시스템 용량. 사용률은 df 와 같은 기준(used / (used + available))으로 낸다.
    shutil 의 total 에는 root 예약 블록이 들어 있어 used/total 로 계산하면
    df 의 Use% 와 몇 % 어긋난다.
    """
    try:
        u = shutil.disk_usage(path)
    except OSError:
        return None
    denom = u.used + u.free
    percent = round(u.used * 100.0 / denom, 1) if denom else 0.0
    return {
        "total": u.total,
        "used": u.used,
        "free": u.free,
        "percent": percent,
        "status": ("danger" if percent >= DANGER_PERCENT
                   else "warn" if percent >= WARN_PERCENT else "ok"),
    }


def database_bytes():
    total = 0
    for suffix in ("", "-wal", "-shm"):
        try:
            total += os.path.getsize(config.DATABASE_PATH + suffix)
        except OSError:
            pass
    return total


# ---------------------------------------------------------------------------
# DB 기준 집계
# ---------------------------------------------------------------------------
def db_counts(db):
    """
    DB 에 기록된 첨부 개수와 크기 합. 디스크 순회 결과와 비교하면 고아 파일을
    발견할 수 있다. (이 화면은 확인만 한다. 삭제 기능은 만들지 않는다)
    """
    def one(sql):
        row = db.execute(sql).fetchone()
        return (row[0] or 0, row[1] or 0)

    chat = one("SELECT COUNT(*), COALESCE(SUM(file_size),0) FROM attachments")
    note = one("SELECT COUNT(*), COALESCE(SUM(file_size),0) FROM note_attachments")
    return {
        "chat_files": chat[0], "chat_bytes": chat[1],
        "note_files": note[0], "note_bytes": note[1],
        "notes": db.execute("SELECT COUNT(*) FROM notes").fetchone()[0],
        "notes_public": db.execute(
            "SELECT COUNT(*) FROM notes WHERE visibility = 'public'").fetchone()[0],
    }


def per_user(db, limit=50):
    """
    사용자별 첨부 사용량. DB 의 file_size 합계로 계산한다. (디스크 순회 불필요)
    소유자가 없는(레거시) 세션/메모는 '(소유자 없음)' 으로 모아 보여준다.
    """
    rows = db.execute(
        "SELECT u.id, u.username, u.display_name,"
        " (SELECT COALESCE(SUM(a.file_size),0) FROM attachments a"
        "    JOIN sessions s ON s.id = a.session_id WHERE s.owner_id = u.id) AS chat_bytes,"
        " (SELECT COUNT(*) FROM attachments a"
        "    JOIN sessions s ON s.id = a.session_id WHERE s.owner_id = u.id) AS chat_files,"
        " (SELECT COALESCE(SUM(a.file_size),0) FROM note_attachments a"
        "    JOIN notes n ON n.id = a.note_id WHERE n.owner_id = u.id) AS note_bytes,"
        " (SELECT COUNT(*) FROM note_attachments a"
        "    JOIN notes n ON n.id = a.note_id WHERE n.owner_id = u.id) AS note_files "
        "FROM users u ORDER BY (chat_bytes + note_bytes) DESC, u.id LIMIT ?",
        (limit,)).fetchall()

    out = []
    for r in rows:
        total = (r["chat_bytes"] or 0) + (r["note_bytes"] or 0)
        if total == 0:
            continue
        out.append({
            "user": r["display_name"] or r["username"],
            "chat_bytes": r["chat_bytes"] or 0, "chat_files": r["chat_files"] or 0,
            "note_bytes": r["note_bytes"] or 0, "note_files": r["note_files"] or 0,
            "total_bytes": total,
        })

    orphan = db.execute(
        "SELECT (SELECT COALESCE(SUM(a.file_size),0) FROM attachments a"
        "          JOIN sessions s ON s.id = a.session_id WHERE s.owner_id IS NULL),"
        "       (SELECT COALESCE(SUM(a.file_size),0) FROM note_attachments a"
        "          JOIN notes n ON n.id = a.note_id WHERE n.owner_id IS NULL)").fetchone()
    if (orphan[0] or 0) + (orphan[1] or 0) > 0:
        out.append({
            "user": "(소유자 없음)",
            "chat_bytes": orphan[0] or 0, "chat_files": 0,
            "note_bytes": orphan[1] or 0, "note_files": 0,
            "total_bytes": (orphan[0] or 0) + (orphan[1] or 0),
        })
    return out


# ---------------------------------------------------------------------------
# 보고서
# ---------------------------------------------------------------------------
def build_report(db):
    started = time.time()
    root = os.path.abspath(os.path.dirname(config.DATABASE_PATH))
    scanned = scan_root(root)

    workdir = (settings_store.get(db, "claude_workdir") or "").strip()

    db_bytes = database_bytes()
    up_bytes, up_files = usage_of(config.UPLOAD_DIR, root, scanned)
    nt_bytes, nt_files = usage_of(config.NOTES_DIR, root, scanned)
    bk_bytes, bk_files = usage_of(config.BACKUP_DIR, root, scanned)
    ws_bytes, ws_files = usage_of(workdir, root, scanned) if workdir else (0, 0)

    # 데이터 루트 안에서 위 항목에 속하지 않는 것 (setup-token.txt 등).
    # 루트 밖에 있는 항목은 합계에서 빠지므로 음수가 되지 않게 0 으로 막는다.
    known = db_bytes + up_bytes + nt_bytes + bk_bytes
    if workdir and os.path.dirname(os.path.abspath(workdir)) == root:
        known += ws_bytes
    other_bytes = max(0, scanned["total"][0] - known)

    app_total = db_bytes + up_bytes + nt_bytes + bk_bytes + ws_bytes + other_bytes

    counts = db_counts(db)
    return {
        # 실제 경로는 하나만 보여준다. (요구사항 30 : 서버 파일 구조를 과도하게
        # 노출하지 않는다. 항목은 논리적 이름으로만 내보낸다)
        "monitor_path": config.STORAGE_MONITOR_PATH,
        "disk": disk_usage(config.STORAGE_MONITOR_PATH),
        "items": [
            {"key": "database", "label": "Database", "bytes": db_bytes, "files": None},
            {"key": "chat", "label": "Chat Attachments",
             "bytes": up_bytes, "files": counts["chat_files"], "disk_files": up_files},
            {"key": "notes", "label": "Note Attachments",
             "bytes": nt_bytes, "files": counts["note_files"], "disk_files": nt_files},
            {"key": "workspace", "label": "Workspace",
             "bytes": ws_bytes, "files": ws_files, "configured": bool(workdir)},
            {"key": "backups", "label": "Backups", "bytes": bk_bytes, "files": bk_files},
            {"key": "other", "label": "Other", "bytes": other_bytes, "files": None},
        ],
        "app_total": app_total,
        "db_recorded": {
            "chat_bytes": counts["chat_bytes"],
            "note_bytes": counts["note_bytes"],
        },
        "notes": {"total": counts["notes"], "public": counts["notes_public"]},
        "per_user": per_user(db),
        "limits": {
            "chat_attachment_mb": config.MAX_UPLOAD_MB,
            "chat_max_files": config.MAX_IMAGES_PER_MESSAGE,
            "note_attachment_mb": config.MAX_NOTE_ATTACHMENT_MB,
            "note_max_files": config.MAX_NOTE_ATTACHMENTS,
        },
        "thresholds": {"warn": WARN_PERCENT, "danger": DANGER_PERCENT},
        "elapsed_ms": int((time.time() - started) * 1000),
        "computed_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }


def report(db, refresh=False):
    """캐시를 거쳐 보고서를 돌려준다. refresh=True 면 다시 계산한다."""
    ttl = max(0, config.STORAGE_CACHE_SECONDS)
    now = time.time()
    with _CACHE_LOCK:
        if not refresh and _CACHE["data"] is not None and (now - _CACHE["at"]) < ttl:
            data = dict(_CACHE["data"])
            data["cached"] = True
            data["cache_age_sec"] = int(now - _CACHE["at"])
            return data

    # 계산은 lock 밖에서 한다. 오래 걸릴 수 있으므로 다른 요청을 막지 않는다.
    data = build_report(db)
    with _CACHE_LOCK:
        _CACHE["at"] = time.time()
        _CACHE["data"] = data
    out = dict(data)
    out["cached"] = False
    out["cache_age_sec"] = 0
    return out
