#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
notes
=====

개인/공유 메모. 채팅과 완전히 독립적인 기능이다.

    제목 / 내용 / 첨부파일 / 공개 범위(private|public)

왜 별도 파일인가
----------------
app.py 는 이미 1000줄에 가깝고 운영 중인 채팅 기능이 들어 있다. 메모를 거기에
끼워 넣으면 리뷰 범위가 채팅 전체로 번진다. 블루프린트로 분리하면 app.py 는
`register_blueprint` 두 줄만 늘어난다.

재사용한 것
-----------
    auth.login_required / check_csrf      로그인과 CSRF (app 의 before_request)
    permissions.can_view_note 등          권한 규칙 (permissions.py 에 함께 둠)
    app.sniff_mime                        이미지 매직바이트 판별 (지연 import)
    db.get_db / audit / ts                DB 접근과 감사 로그

저장 구조
---------
    NOTES_DIR/user_<owner_id>/note_<note_id>/<uuid>.<ext>

DB(note_attachments.file_path)에는 **NOTES_DIR 기준 상대경로**만 넣는다.
채팅 첨부(v2->v3 마이그레이션)와 같은 규칙이다. 절대경로를 넣으면 DB 를 다른
서버로 옮길 때 첨부가 전부 열리지 않는다.

보안
----
- 첨부는 DB id 로만 내려준다. 파일 경로를 URL 파라미터로 받지 않는다.
- 내려주기 전에 실제 경로가 NOTES_DIR 안인지 다시 확인한다. (심볼릭 링크 포함)
- private 메모와 그 첨부는 owner 만 접근할 수 있다. 목록은 SQL 단계에서 걸러
  프론트에서 숨기는 방식에 의존하지 않는다.
- 사용자가 준 파일명은 저장 경로 계산에 쓰지 않는다. (`../../etc/passwd` 무해)
- 확장자 + 내용(매직바이트) + 선언 MIME 을 모두 검사한다. 실행 파일/스크립트는
  확장자 자체가 허용 목록에 없다.
"""

import os
import uuid

from flask import (
    Blueprint, abort, jsonify, render_template, request, send_file,
)

import auth
import config
import permissions
from db import audit, get_db, row_to_dict, ts

bp = Blueprint("notes", __name__)
api = Blueprint("notes_api", __name__, url_prefix="/api")

NOTES_DIR = config.NOTES_DIR
MAX_BYTES = config.MAX_NOTE_ATTACHMENT_MB * 1024 * 1024


# ---------------------------------------------------------------------------
# 경로
# ---------------------------------------------------------------------------
def note_dir(owner_id, note_id):
    """소유자가 없는(레거시) 메모도 한 곳에 모이도록 user_0 을 쓴다."""
    path = os.path.join(NOTES_DIR, "user_%d" % (owner_id or 0), "note_%d" % note_id)
    os.makedirs(path, exist_ok=True)
    return path


def rel_note_path(full_path):
    """DB 에 넣을 값. NOTES_DIR 기준 상대경로. 구분자는 항상 '/'."""
    return os.path.relpath(os.path.abspath(full_path), NOTES_DIR).replace("\\", "/")


def abs_note_path(stored):
    """DB 의 file_path 를 실제 경로로 되돌린다. 과거 절대경로 값도 받아준다."""
    value = str(stored or "")
    if value.startswith(("/", "\\")) or (len(value) > 2 and value[1] == ":"):
        return value
    return os.path.join(NOTES_DIR, value.replace("/", os.sep))


def inside_notes_dir(path):
    """심볼릭 링크를 따라간 실제 경로가 NOTES_DIR 안인지 확인한다."""
    real = os.path.realpath(path)
    root = os.path.realpath(NOTES_DIR)
    try:
        return os.path.commonpath([real, root]) == root
    except ValueError:  # 드라이브가 다르면 commonpath 가 예외를 낸다
        return False


# ---------------------------------------------------------------------------
# 업로드 검증
# ---------------------------------------------------------------------------
def looks_like_text(blob):
    """
    txt 는 매직바이트가 없다. 그래서 "실행될 수 없는 평문"인지만 확인한다.
      - NUL 바이트가 있으면 바이너리다. (ELF/PE/압축파일 등)
      - UTF-8 로 해석되어야 한다. (한글 메모를 위해 필요)
    앞부분만 보므로 완벽한 판별은 아니지만, .txt 로 위장한 실행 파일을 걸러낸다.
    확장자가 txt 인 파일은 브라우저가 실행하지 않고, 아래에서 Content-Type 을
    text/plain 으로 고정하고 nosniff 를 붙여 내려준다.
    """
    if b"\x00" in blob:
        return False
    try:
        blob.decode("utf-8")
    except UnicodeDecodeError:
        # 멀티바이트 문자가 읽은 경계에서 잘렸을 수 있다. 마지막 3바이트를 버리고 재시도.
        try:
            blob[:-3].decode("utf-8")
        except UnicodeDecodeError:
            return False
    return True


def sniff_note_file(head, ext):
    """
    파일 내용으로 MIME 을 판별한다. 확장자와 맞지 않으면 None.
    이미지 판별은 채팅과 같은 함수를 쓴다. (app.sniff_mime)
    """
    # 지연 import : app.py 가 이 모듈을 import 하므로 최상단에서 하면 순환이 된다.
    # (admin.py 가 remove_tree 를 가져오는 방식과 같다)
    from app import sniff_mime

    if ext == "pdf":
        return "application/pdf" if head.startswith(b"%PDF-") else None
    if ext == "txt":
        return "text/plain" if looks_like_text(head) else None
    return sniff_mime(head)


def save_note_upload(storage, owner_id, note_id):
    """(info, error) 를 돌려준다. 저장에 성공하면 error 는 None."""
    original = os.path.basename((storage.filename or "").replace("\\", "/")) or "file"
    ext = original.rsplit(".", 1)[-1].lower() if "." in original else ""
    allowed = config.ALLOWED_NOTE_FILES
    if ext not in allowed:
        return None, "허용되지 않는 파일 형식입니다: %s (허용: %s)" % (
            ext or "(확장자 없음)", ", ".join(sorted(allowed)))

    storage.stream.seek(0, os.SEEK_END)
    size = storage.stream.tell()
    storage.stream.seek(0)
    if size <= 0:
        return None, "빈 파일입니다: %s" % original
    if size > MAX_BYTES:
        return None, "파일이 너무 큽니다: %s (%.1fMB / 최대 %dMB)" % (
            original, size / 1048576.0, config.MAX_NOTE_ATTACHMENT_MB)

    # txt 판별을 위해 넉넉히 읽는다. 이미지/PDF 는 앞 8바이트로 충분하다.
    head = storage.stream.read(4096)
    storage.stream.seek(0)

    sniffed = sniff_note_file(head, ext)
    if sniffed is None:
        return None, "확장자와 실제 파일 내용이 다릅니다: %s" % original
    if sniffed not in allowed[ext]:
        return None, "확장자와 실제 파일 내용이 다릅니다: %s (내용=%s)" % (original, sniffed)

    declared = (storage.mimetype or "").lower()
    if declared and declared != sniffed and declared != "application/octet-stream":
        # txt 는 브라우저/OS 마다 선언 MIME 이 제각각이라(text/*) 관대하게 본다.
        if not (ext == "txt" and declared.startswith("text/")):
            return None, "MIME 타입이 올바르지 않습니다: %s (%s)" % (original, declared)

    stored_name = "%s.%s" % (uuid.uuid4().hex, ext)
    full_path = os.path.join(note_dir(owner_id, note_id), stored_name)
    storage.save(full_path)
    return {
        "original_name": original[:255],
        "stored_name": stored_name,
        "file_path": rel_note_path(full_path),
        "abs_path": os.path.abspath(full_path),
        "mime_type": sniffed,
        "file_size": size,
    }, None


def discard_saved(saved):
    for s in saved:
        try:
            os.remove(s["abs_path"])
        except OSError:
            pass


# ---------------------------------------------------------------------------
# 조회 / 직렬화
# ---------------------------------------------------------------------------
def get_note_row(db, nid):
    return db.execute(
        "SELECT n.*, u.username AS owner_username, u.display_name AS owner_display_name "
        "FROM notes n LEFT JOIN users u ON u.id = n.owner_id WHERE n.id = ?",
        (nid,)).fetchone()


def get_note_or_404(db, nid):
    row = get_note_row(db, nid)
    if row is None:
        abort(404, "메모를 찾을 수 없습니다.")
    return row


def attachment_rows(db, note_id):
    return db.execute(
        "SELECT id, original_name, mime_type, file_size, created_at "
        "FROM note_attachments WHERE note_id = ? ORDER BY id", (note_id,)).fetchall()


def note_payload(user, row, attachments=None, with_content=True):
    d = row_to_dict(row)
    keys = row.keys()
    if "owner_display_name" in keys:
        d["owner_name"] = d.get("owner_display_name") or d.get("owner_username")
    d.pop("owner_display_name", None)
    d.pop("owner_username", None)
    d["is_owner"] = permissions.is_note_owner(user, row)
    d["can_manage"] = permissions.can_manage_note(user, row)
    if not with_content:
        # 목록에서는 본문 전체를 보내지 않는다. (private 본문이 필요 이상으로
        # 돌아다니지 않게 하고, 목록 응답도 가볍게 유지한다)
        content = d.pop("content", "") or ""
        d["preview"] = " ".join(content.split())[:120]
    if attachments is not None:
        d["attachments"] = [row_to_dict(a) for a in attachments]
        d["attachment_count"] = len(attachments)
    return d


def limits():
    return {
        "max_note_attachment_mb": config.MAX_NOTE_ATTACHMENT_MB,
        "max_note_attachments": config.MAX_NOTE_ATTACHMENTS,
        "max_title_chars": config.MAX_NOTE_TITLE_CHARS,
        "max_content_chars": config.MAX_NOTE_CONTENT_CHARS,
        "allowed_ext": sorted(config.ALLOWED_NOTE_FILES),
    }


# ---------------------------------------------------------------------------
# 화면
# ---------------------------------------------------------------------------
@bp.get("/notes")
@auth.login_required
def page():
    user = auth.current_user()
    return render_template(
        "notes.html",
        csrf=auth.csrf_token(),
        me=auth.public_user(user),
        is_admin=permissions.is_admin(user),
        rail="notes",          # 왼쪽 레일에서 지금 보고 있는 곳
        limits=limits(),
    )


# ---------------------------------------------------------------------------
# 메모 API
# ---------------------------------------------------------------------------
def _read_fields(required_title):
    """
    JSON 과 multipart/form-data 를 모두 받는다.
    반환: (title, content, visibility, files, present)
    present 는 "요청에 그 키가 들어 있었는가" 다. PATCH 의 부분 수정을 위해 필요하다.
    """
    if request.files or request.form:
        src, files = request.form, [f for f in request.files.getlist("files")
                                    if f and f.filename]
    else:
        src, files = (request.get_json(silent=True) or {}), []

    present = set(k for k in src.keys() if k in ("title", "content", "visibility"))

    title = src.get("title", "")
    content = src.get("content", "")
    visibility = src.get("visibility", "")
    if not isinstance(title, str) or not isinstance(content, str) \
            or not isinstance(visibility, str):
        abort(400, "잘못된 요청 형식입니다.")

    title = title.replace("\r\n", "\n").strip()
    content = content.replace("\r\n", "\n").strip()
    visibility = visibility.strip().lower()

    if len(title) > config.MAX_NOTE_TITLE_CHARS:
        abort(400, "제목이 너무 깁니다. (최대 %d자)" % config.MAX_NOTE_TITLE_CHARS)
    if len(content) > config.MAX_NOTE_CONTENT_CHARS:
        abort(413, "내용이 너무 깁니다. (%d자 / 최대 %d자)"
              % (len(content), config.MAX_NOTE_CONTENT_CHARS))
    if visibility and visibility not in permissions.VISIBILITIES:
        abort(400, "공개 범위가 올바르지 않습니다.")
    if required_title and not title:
        abort(400, "제목을 입력해 주세요.")
    if len(files) > config.MAX_NOTE_ATTACHMENTS:
        abort(400, "첨부파일은 한 번에 최대 %d개까지 올릴 수 있습니다. (요청 %d개)"
              % (config.MAX_NOTE_ATTACHMENTS, len(files)))
    return title, content, visibility, files, present


@api.get("/notes")
@auth.login_required
def list_notes():
    """
    scope : all(기본) | mine | shared
    q     : 제목 LIKE 검색
    권한 필터는 SQL 단계에서 걸린다.
    """
    db = get_db()
    user = auth.current_user()

    scope = (request.args.get("scope") or "all").lower()
    if scope not in ("all", "mine", "shared"):
        scope = "all"
    where, params = permissions.visible_notes_clause(user, scope)

    sql = ("SELECT n.*, u.username AS owner_username, u.display_name AS owner_display_name,"
           " (SELECT COUNT(*) FROM note_attachments a WHERE a.note_id = n.id) AS attachment_count "
           "FROM notes n LEFT JOIN users u ON u.id = n.owner_id "
           "WHERE " + where)
    args = list(params)

    q = (request.args.get("q") or "").strip()
    if q:
        sql += " AND n.title LIKE ? ESCAPE '\\'"
        args.append("%" + q.replace("\\", "\\\\").replace("%", "\\%")
                    .replace("_", "\\_") + "%")

    sql += " ORDER BY n.updated_at DESC, n.id DESC LIMIT 500"
    rows = db.execute(sql, args).fetchall()

    out = []
    for r in rows:
        d = note_payload(user, r, with_content=False)
        d["attachment_count"] = r["attachment_count"]
        out.append(d)
    return jsonify(ok=True, notes=out, scope=scope, limits=limits())


@api.post("/notes")
@auth.login_required
def create_note():
    db = get_db()
    user = auth.current_user()
    title, content, visibility, files, _ = _read_fields(required_title=True)
    visibility = visibility or permissions.PRIVATE

    now = ts()
    cur = db.execute(
        "INSERT INTO notes (owner_id, title, content, visibility, created_at, updated_at)"
        " VALUES (?,?,?,?,?,?)",
        (user["id"], title, content, visibility, now, now))
    nid = cur.lastrowid

    # 파일은 DB 기록 전에 검증/저장한다. 하나라도 실패하면 전부 되돌린다.
    saved = []
    for f in files:
        info, err = save_note_upload(f, user["id"], nid)
        if err:
            discard_saved(saved)
            db.rollback()
            abort(400, err)
        saved.append(info)
    for s in saved:
        db.execute(
            "INSERT INTO note_attachments (note_id, original_name, stored_name,"
            " file_path, mime_type, file_size, created_at) VALUES (?,?,?,?,?,?,?)",
            (nid, s["original_name"], s["stored_name"], s["file_path"],
             s["mime_type"], s["file_size"], now))

    # 본문은 남기지 않는다. 공개 범위 변경만 추적 대상이다.
    audit(db, user["id"], "note_created", "note", nid, "visibility=%s" % visibility)
    db.commit()

    row = get_note_row(db, nid)
    return jsonify(ok=True, note=note_payload(user, row, attachment_rows(db, nid))), 201


@api.get("/notes/<int:nid>")
@auth.login_required
def get_note(nid):
    db = get_db()
    user = auth.current_user()
    row = get_note_or_404(db, nid)
    permissions.require_view_note(user, row)
    return jsonify(ok=True, note=note_payload(user, row, attachment_rows(db, nid)))


@api.patch("/notes/<int:nid>")
@auth.login_required
def update_note(nid):
    db = get_db()
    user = auth.current_user()
    row = get_note_or_404(db, nid)
    permissions.require_manage_note(user, row)

    title, content, visibility, files, present = _read_fields(required_title=False)

    fields, values, changed_visibility = [], [], None
    if "title" in present:
        if not title:
            abort(400, "제목을 입력해 주세요.")
        fields.append("title = ?")
        values.append(title)
    if "content" in present:
        fields.append("content = ?")
        values.append(content)
    if "visibility" in present and visibility:
        fields.append("visibility = ?")
        values.append(visibility)
        changed_visibility = visibility
    if not fields and not files:
        abort(400, "변경할 내용이 없습니다.")

    existing = db.execute(
        "SELECT COUNT(*) FROM note_attachments WHERE note_id = ?", (nid,)).fetchone()[0]
    if files and existing + len(files) > config.MAX_NOTE_ATTACHMENTS:
        abort(400, "첨부파일은 메모당 최대 %d개까지입니다. (현재 %d개)"
              % (config.MAX_NOTE_ATTACHMENTS, existing))

    now = ts()
    saved = []
    for f in files:
        info, err = save_note_upload(f, row["owner_id"], nid)
        if err:
            discard_saved(saved)
            abort(400, err)
        saved.append(info)

    try:
        if fields:
            fields.append("updated_at = ?")
            values += [now, nid]
            db.execute("UPDATE notes SET %s WHERE id = ?" % ", ".join(fields), values)
        for s in saved:
            db.execute(
                "INSERT INTO note_attachments (note_id, original_name, stored_name,"
                " file_path, mime_type, file_size, created_at) VALUES (?,?,?,?,?,?,?)",
                (nid, s["original_name"], s["stored_name"], s["file_path"],
                 s["mime_type"], s["file_size"], now))
        if saved and not fields:
            db.execute("UPDATE notes SET updated_at = ? WHERE id = ?", (now, nid))
        if changed_visibility:
            audit(db, user["id"], "note_visibility_changed", "note", nid,
                  "to=%s" % changed_visibility)
        db.commit()
    except Exception:
        db.rollback()
        discard_saved(saved)
        raise

    fresh = get_note_row(db, nid)
    return jsonify(ok=True, note=note_payload(user, fresh, attachment_rows(db, nid)))


@api.delete("/notes/<int:nid>")
@auth.login_required
def delete_note(nid):
    db = get_db()
    user = auth.current_user()
    row = get_note_or_404(db, nid)
    permissions.require_manage_note(user, row)

    # DB 먼저 정리(ON DELETE CASCADE) -> 커밋 후 파일 정리.
    # 파일 삭제가 실패해도 DB 트랜잭션에는 영향이 없다. (채팅 삭제와 같은 순서)
    db.execute("DELETE FROM notes WHERE id = ?", (nid,))
    audit(db, user["id"], "note_deleted", "note", nid)
    db.commit()

    from app import remove_tree  # 순환 import 방지를 위해 지연 import
    target = os.path.join(NOTES_DIR, "user_%d" % (row["owner_id"] or 0),
                          "note_%d" % nid)
    files_ok = remove_tree(target) if inside_notes_dir(target) else False
    return jsonify(ok=True, deleted=nid, files_removed=files_ok)


# ---------------------------------------------------------------------------
# 첨부파일 API
# ---------------------------------------------------------------------------
@api.get("/note-attachments/<int:aid>")
@auth.login_required
def get_note_attachment(aid):
    db = get_db()
    user = auth.current_user()
    row = db.execute(
        "SELECT * FROM note_attachments WHERE id = ?", (aid,)).fetchone()
    if row is None:
        abort(404, "첨부파일을 찾을 수 없습니다.")

    note = get_note_row(db, row["note_id"])
    if note is None:
        abort(404, "첨부파일을 찾을 수 없습니다.")
    # private 메모의 첨부는 owner 만. URL 을 알아도 접근할 수 없다.
    permissions.require_view_note(user, note)

    path = abs_note_path(row["file_path"])
    if not inside_notes_dir(path):
        abort(403, "허용되지 않는 경로입니다.")
    real = os.path.realpath(path)
    if not os.path.isfile(real):
        abort(404, "파일이 존재하지 않습니다.")

    # 이미지와 PDF 는 브라우저에서 바로 보여주고, 그 외(txt)는 내려받게 한다.
    # txt 를 인라인으로 렌더하면 같은 오리진에서 임의 텍스트가 열리는 셈이 되므로
    # 다운로드로 강제하고 Content-Type 도 고정한다.
    inline = row["mime_type"] in ("image/png", "image/jpeg", "image/webp",
                                  "image/gif", "application/pdf")
    resp = send_file(real, mimetype=row["mime_type"],
                     download_name=row["original_name"], as_attachment=not inline)
    resp.headers["Cache-Control"] = "private, no-store"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    return resp


@api.delete("/note-attachments/<int:aid>")
@auth.login_required
def delete_note_attachment(aid):
    db = get_db()
    user = auth.current_user()
    row = db.execute(
        "SELECT * FROM note_attachments WHERE id = ?", (aid,)).fetchone()
    if row is None:
        abort(404, "첨부파일을 찾을 수 없습니다.")
    note = get_note_row(db, row["note_id"])
    if note is None:
        abort(404, "첨부파일을 찾을 수 없습니다.")
    permissions.require_manage_note(user, note)

    path = abs_note_path(row["file_path"])
    db.execute("DELETE FROM note_attachments WHERE id = ?", (aid,))
    db.execute("UPDATE notes SET updated_at = ? WHERE id = ?", (ts(), note["id"]))
    db.commit()

    removed = False
    if inside_notes_dir(path):
        try:
            os.remove(os.path.realpath(path))
            removed = True
        except OSError:
            pass
    return jsonify(ok=True, deleted=aid, file_removed=removed)
