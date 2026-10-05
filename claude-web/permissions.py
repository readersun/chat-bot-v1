#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
permissions
===========

권한 규칙을 한 곳에 모은다. 라우트마다 조건을 다시 쓰지 않는다.

규칙 요약
---------
                    조회   작성   이름/공개범위 변경   삭제
private  owner       O      O            O             O
         그 외       X      X            X             X
public   owner       O      O            O             O
         로그인자    O      O            X             X

admin
  - 사용자/시스템 설정/프로젝트 관리 가능
  - **private 세션의 대화 내용은 볼 수 없다.** (요구사항 27)
    "나만 보기" 가 실제로 나만 보기여야 하므로 can_view_session 에서 admin 을
    특별 취급하지 않는다. 관리자는 메타데이터(목록/소유자/크기)만 다루는
    별도 엔드포인트(/api/admin/sessions)를 쓴다.

session_members 는 지금은 비어 있지만(향후 shared 용) 규칙에는 이미 반영해
두었다. 나중에 shared 를 붙일 때 이 파일만 고치면 된다.

메모(notes)의 규칙은 아래 "메모" 절에 따로 있다. 세션과 거의 같지만 public 메모를
남이 고칠 수 없다는 점이 다르다.
"""

from flask import abort

PRIVATE = "private"
PUBLIC = "public"
VISIBILITIES = (PRIVATE, PUBLIC)


def is_admin(user):
    return bool(user) and user["role"] == "admin"


def is_owner(user, sess):
    if not user or sess is None:
        return False
    owner_id = sess["owner_id"]
    return owner_id is not None and owner_id == user["id"]


def member_permission(db, session_id, user_id):
    """향후 shared 용. 지금은 항상 None 이다."""
    if not user_id:
        return None
    row = db.execute(
        "SELECT permission FROM session_members WHERE session_id = ? AND user_id = ?",
        (session_id, user_id)).fetchone()
    return row["permission"] if row else None


# ---------------------------------------------------------------------------
# 판정
# ---------------------------------------------------------------------------
def can_view_session(db, user, sess):
    if not user or sess is None:
        return False
    if is_owner(user, sess):
        return True
    if sess["visibility"] == PUBLIC:
        return True
    return member_permission(db, sess["id"], user["id"]) in ("read", "write")


def can_write_session(db, user, sess):
    """메시지를 추가할 수 있는가. public 은 로그인 사용자 누구나 가능하다."""
    if not user or sess is None:
        return False
    if is_owner(user, sess):
        return True
    if sess["visibility"] == PUBLIC:
        return True
    return member_permission(db, sess["id"], user["id"]) == "write"


def can_manage_session(user, sess):
    """이름 변경 / 공개범위 변경 / 삭제. 소유자만."""
    return is_owner(user, sess)


def can_view_project(user):
    """프로젝트는 공용 그룹이므로 로그인 사용자면 모두 볼 수 있다."""
    return bool(user)


def can_manage_project(user):
    """프로젝트 생성/이름변경/삭제는 관리자만. (공용 목록이 무분별하게 늘지 않게)"""
    return is_admin(user)


# ---------------------------------------------------------------------------
# require_* : 실패하면 곧바로 HTTP 오류
# ---------------------------------------------------------------------------
def require_admin(user):
    if not user:
        abort(401, "로그인이 필요합니다.")
    if not is_admin(user):
        abort(403, "관리자 권한이 필요합니다.")


def require_view_session(db, user, sess):
    if not can_view_session(db, user, sess):
        # 존재 여부까지 숨긴다. private 세션의 id 를 찍어봐도 404 만 보인다.
        abort(404, "세션을 찾을 수 없습니다.")


def require_write_session(db, user, sess):
    if not can_view_session(db, user, sess):
        abort(404, "세션을 찾을 수 없습니다.")
    if not can_write_session(db, user, sess):
        abort(403, "이 세션에 메시지를 작성할 권한이 없습니다.")


def require_manage_session(db, user, sess):
    if not can_view_session(db, user, sess):
        abort(404, "세션을 찾을 수 없습니다.")
    if not can_manage_session(user, sess):
        abort(403, "세션 소유자만 변경하거나 삭제할 수 있습니다.")


def require_manage_project(user):
    if not user:
        abort(401, "로그인이 필요합니다.")
    if not can_manage_project(user):
        abort(403, "프로젝트 관리는 관리자만 할 수 있습니다.")


# ---------------------------------------------------------------------------
# 메모 (notes)
#
# 세션과 규칙이 다른 점이 하나 있다. **public 메모는 다른 사람이 수정할 수 없다.**
# 세션은 여러 사람이 함께 대화하는 공간이라 public 이면 쓰기를 허용하지만,
# 메모는 작성자의 글이므로 owner 만 고칠 수 있다.
#
#                     조회   수정/삭제/공개범위 변경
#   private  owner     O              O
#            그 외     X              X
#   public   owner     O              O
#            로그인자  O              X
#
# admin 도 특별 취급하지 않는다. private 메모는 관리자에게도 보이지 않는다.
# (세션과 같은 원칙. 관리자는 Storage 화면에서 용량 합계만 본다)
# ---------------------------------------------------------------------------
def is_note_owner(user, note):
    if not user or note is None:
        return False
    owner_id = note["owner_id"]
    return owner_id is not None and owner_id == user["id"]


def can_view_note(user, note):
    if not user or note is None:
        return False
    if is_note_owner(user, note):
        return True
    return note["visibility"] == PUBLIC


def can_manage_note(user, note):
    """수정 / 삭제 / 공개범위 변경 / 첨부 추가·삭제. 소유자만."""
    return is_note_owner(user, note)


def require_view_note(user, note):
    if not can_view_note(user, note):
        # 존재 여부까지 숨긴다. 남의 private 메모 id 를 찍어봐도 404 만 보인다.
        abort(404, "메모를 찾을 수 없습니다.")


def require_manage_note(user, note):
    if not can_view_note(user, note):
        abort(404, "메모를 찾을 수 없습니다.")
    if not can_manage_note(user, note):
        abort(403, "메모 작성자만 수정하거나 삭제할 수 있습니다.")


# ---------------------------------------------------------------------------
# 메모 댓글 (note_comments)
#
# 댓글은 "그 메모를 볼 수 있는 사람" 의 것이다. 메모 본문의 수정 권한과는
# 다르게 본다. 공개 메모는 읽으라고 공개한 것이고, 읽은 사람이 한 마디
# 남기는 것이 댓글의 쓸모이기 때문이다.
#
#                     댓글 읽기   댓글 쓰기   내 댓글 수정   댓글 삭제
#   private  owner        O           O            O            O
#            그 외        X           X            -            -
#   public   owner        O           O            O         O (남의 것도)
#            로그인자     O           O            O         O (자기 것만)
#
# 삭제를 메모 주인에게도 허용하는 이유: 자기 메모 아래 달린 글을 정리할 수
# 있어야 한다. 반대로 **수정은 작성자만** 한다. 남의 말을 고쳐 쓰는 일은
# 허용하지 않는다.
#
# admin 은 여기서도 특별 취급하지 않는다. private 메모의 댓글은 관리자에게도
# 보이지 않는다. (메모 본문과 같은 원칙)
# ---------------------------------------------------------------------------
def is_comment_author(user, comment):
    if not user or comment is None:
        return False
    uid = comment["user_id"]
    return uid is not None and uid == user["id"]


def can_write_comment(user, note):
    """댓글을 달 수 있는가. 그 메모를 볼 수 있으면 된다."""
    return can_view_note(user, note)


def can_edit_comment(user, comment):
    """내용을 고치는 것은 쓴 사람만."""
    return is_comment_author(user, comment)


def can_delete_comment(user, note, comment):
    """지우는 것은 쓴 사람 또는 메모 주인."""
    return is_comment_author(user, comment) or is_note_owner(user, note)


def require_write_comment(user, note):
    if not can_view_note(user, note):
        abort(404, "메모를 찾을 수 없습니다.")
    if not can_write_comment(user, note):
        abort(403, "이 메모에는 댓글을 쓸 수 없습니다.")


def require_edit_comment(user, note, comment):
    if not can_view_note(user, note):
        abort(404, "메모를 찾을 수 없습니다.")
    if not can_edit_comment(user, comment):
        abort(403, "댓글을 쓴 사람만 고칠 수 있습니다.")


def require_delete_comment(user, note, comment):
    if not can_view_note(user, note):
        abort(404, "메모를 찾을 수 없습니다.")
    if not can_delete_comment(user, note, comment):
        abort(403, "댓글을 쓴 사람이나 메모 작성자만 지울 수 있습니다.")


# ---------------------------------------------------------------------------
# 목록 조회용 SQL 조각
# ---------------------------------------------------------------------------
def visible_notes_clause(user, scope="all"):
    """
    메모 목록을 DB 단계에서 걸러낸다. (프론트에서 숨기는 방식이 아니다)
    반환: (where 조각, 파라미터 list). 테이블 별칭은 n 이다.
    scope : all | mine | shared
      mine   내가 쓴 것 (private + public 모두)
      shared 다른 사람의 public
    """
    uid = user["id"]
    if scope == "mine":
        return "n.owner_id = ?", [uid]
    if scope == "shared":
        return ("(n.visibility = 'public' AND (n.owner_id IS NULL OR n.owner_id != ?))",
                [uid])
    return "(n.owner_id = ? OR n.visibility = 'public')", [uid]


def visible_sessions_clause(user, scope="all"):
    """
    세션 목록을 DB 단계에서 걸러낸다. (프론트에서 숨기는 방식이 아니다)
    반환: (where 조각, 파라미터 list)
    scope : all | mine | public
    """
    uid = user["id"]
    if scope == "mine":
        return "s.owner_id = ?", [uid]
    if scope == "public":
        # 공개 세션 중 내 것이 아닌 것 (목록 중복 방지)
        return "(s.visibility = 'public' AND (s.owner_id IS NULL OR s.owner_id != ?))", [uid]
    return (
        "(s.owner_id = ? OR s.visibility = 'public' "
        " OR EXISTS (SELECT 1 FROM session_members sm "
        "            WHERE sm.session_id = s.id AND sm.user_id = ?))",
        [uid, uid],
    )


# ---------------------------------------------------------------------------
# 메뉴 권한 (v6)
#
# "문이 열리는가" 를 정한다. 그 안에서 무엇이 보이는지는 위의 세션/메모 규칙이
# 따로 정한다. 두 겹이고 서로 섞이지 않는다. notes 메뉴가 있어도 남의 private
# 메모는 여전히 안 보인다.
#
# 레일에서 항목을 안 그리는 것은 권한이 아니다. 주소를 직접 치면 들어간다.
# 그래서 화면과 API 양쪽에 require_menu 를 건다.
# ---------------------------------------------------------------------------
MENUS = ("chat", "notes", "patch", "servers")

MENU_LABELS = {"chat": "채팅", "notes": "메모", "patch": "패치", "servers": "서버"}

# 메뉴를 하나도 못 받은 사람을 어디로 보낼지. 가진 것 중 첫 번째다.
MENU_PATHS = {"chat": "/", "notes": "/notes", "patch": "/patch",
              "servers": "/servers"}

# 신규 사용자 기본값. 패치는 고객사에 나가는 바이너리라 기본으로 열지 않는다.
DEFAULT_NEW_USER_MENUS = ("chat",)


def user_menus(db, user):
    """그 사람이 볼 수 있는 메뉴 집합. 관리자는 항상 전부 가진다."""
    if not user:
        return set()
    if is_admin(user):
        # 관리자가 자기 메뉴를 다 꺼서 스스로 갇히는 일을 막는다.
        return set(MENUS)
    rows = db.execute("SELECT menu_key FROM user_menus WHERE user_id = ?",
                      (user["id"],)).fetchall()
    return {r["menu_key"] for r in rows if r["menu_key"] in MENUS}


def has_menu(db, user, key):
    return key in user_menus(db, user)


def require_menu(db, user, key):
    if not user:
        abort(401, "로그인이 필요합니다.")
    if not has_menu(db, user, key):
        abort(403, "%s 메뉴를 쓸 권한이 없습니다. 관리자에게 요청하세요."
              % MENU_LABELS.get(key, key))


def landing_path(db, user):
    """로그인 직후 보낼 곳. 가진 메뉴가 하나도 없으면 None."""
    mine = user_menus(db, user)
    for key in MENUS:          # chat -> notes -> patch 순서 고정
        if key in mine:
            return MENU_PATHS[key]
    return None


def set_user_menus(db, user_id, keys, granted_by=None):
    """그 사람의 메뉴를 통째로 교체한다. 돌려주는 값은 실제로 남은 집합."""
    from db import ts
    keep = {k for k in keys if k in MENUS}
    db.execute("DELETE FROM user_menus WHERE user_id = ?", (user_id,))
    if keep:
        db.executemany(
            "INSERT INTO user_menus (user_id, menu_key, granted_at, granted_by)"
            " VALUES (?,?,?,?)",
            [(user_id, k, ts(), granted_by) for k in sorted(keep)])
    return keep


# ---------------------------------------------------------------------------
# 패치 사이트 범위 (v6)
#
# patch 메뉴 하나로 모든 고객사의 패치가 보인다. 사이트가 하나일 때는 문제가
# 아니었지만 여러 고객사 폴더가 한 서버에 놓이면 A사 담당자가 B사에 나간
# 버전을 받아 갈 수 있다. 그래서 범위를 하나 더 둔다.
#
#   users.patch_all_sites = 1  전체 (기본값, 지금 동작 그대로)
#                          = 0  user_patch_sites 에 고른 것만
#
# 거르는 지점은 목록 질의와 다운로드 두 곳이다. 목록에 안 보이는 것은
# 다운로드도 404 다.
# ---------------------------------------------------------------------------
def patch_all_sites(user):
    if not user:
        return False
    if is_admin(user):
        return True
    try:
        return bool(user["patch_all_sites"])
    except (IndexError, KeyError):
        return True        # 마이그레이션 전 행. 지금 동작을 바꾸지 않는다.


def allowed_site_ids(db, user):
    """볼 수 있는 사이트 id 집합. None 이면 '전부' 라는 뜻이다."""
    if patch_all_sites(user):
        return None
    rows = db.execute("SELECT site_id FROM user_patch_sites WHERE user_id = ?",
                      (user["id"],)).fetchall()
    return {r["site_id"] for r in rows}


def visible_sites_clause(db, user, alias="s"):
    """
    사이트 목록을 DB 단계에서 걸러낸다. 반환: (where 조각, 파라미터 list).

    숨긴 사이트는 누구에게도 안 보인다. 관리자도 여기서는 못 본다.
    관리자가 숨긴 것을 보는 자리는 /admin/patch 다. 사용자 화면에서 관리자만
    더 보이면 "나한테는 보이는데요" 로 끝나는 문의가 생긴다.
    """
    where = ["%s.is_visible = 1" % alias]
    params = []
    ids = allowed_site_ids(db, user)
    if ids is not None:
        if not ids:
            return "0", []           # 고른 사이트가 하나도 없다
        marks = ",".join("?" for _ in ids)
        where.append("%s.id IN (%s)" % (alias, marks))
        params.extend(sorted(ids))
    return " AND ".join(where), params


def can_view_site(db, user, site_row):
    if site_row is None or not site_row["is_visible"]:
        return False
    ids = allowed_site_ids(db, user)
    return ids is None or site_row["id"] in ids


def set_user_patch_sites(db, user_id, all_sites, site_ids, granted_by=None):
    """사이트 범위를 통째로 교체한다."""
    from db import ts
    db.execute("UPDATE users SET patch_all_sites = ? WHERE id = ?",
               (1 if all_sites else 0, user_id))
    db.execute("DELETE FROM user_patch_sites WHERE user_id = ?", (user_id,))
    keep = []
    if not all_sites:
        for sid in sorted(set(int(x) for x in site_ids)):
            row = db.execute("SELECT id FROM patch_sites WHERE id = ?", (sid,)).fetchone()
            if row:
                keep.append(sid)
        if keep:
            db.executemany(
                "INSERT INTO user_patch_sites (user_id, site_id, granted_at, granted_by)"
                " VALUES (?,?,?,?)",
                [(user_id, sid, ts(), granted_by) for sid in keep])
    return keep


# ---------------------------------------------------------------------------
# SSH 중계 : 사용 허용 (v8)
#
# 겹이 셋이다. 섞이지 않는다.
#
#   1. servers 메뉴          문이 열리는가            (user_menus)
#   2. 등급 ssh_level        조회만인가 변경까지인가  (users.ssh_level)
#   3. 범위 ssh_all_servers  어느 서버까지인가        (users / ssh_grants)
#
# 등급에는 천장이 하나 더 있다. 관리자 화면의 기본 정책(relay_policy)이다.
# 사람마다 '조회+변경' 을 받아 뒀어도 정책이 '조회만' 이면 변경 명령은 나가지
# 않는다. 둘 중 낮은 것이 이긴다. 정책 하나로 전사를 되돌릴 수 있어야 한다.
#
# 등급과 승인은 **챗봇이 스스로 고른 명령에만** 걸린다. 웹 터미널에서 사람이
# 직접 치는 줄은 등급을 매기지 않는다. 브라우저가 PuTTY 를 대신하는 것일 뿐이고
# 그 사람의 계정 권한이 늘지 않는다. (기록은 양쪽 다 남긴다)
# ---------------------------------------------------------------------------
SSH_OFF = "off"
SSH_READ = "read"
SSH_WRITE = "write"
SSH_LEVELS = (SSH_OFF, SSH_READ, SSH_WRITE)

_SSH_RANK = {SSH_OFF: 0, SSH_READ: 1, SSH_WRITE: 2}

SSH_LEVEL_LABELS = {
    SSH_OFF: "꺼짐",
    SSH_READ: "조회만",
    SSH_WRITE: "조회 + 변경(승인받고)",
}


def ssh_policy(db):
    """시스템 전체 천장. 'read' 또는 'write'. 기본은 조회만이다."""
    import settings_store
    v = (settings_store.get(db, "relay_policy", SSH_READ) or SSH_READ).strip().lower()
    return v if v in (SSH_READ, SSH_WRITE) else SSH_READ


def _stored_ssh_level(user):
    try:
        v = (user["ssh_level"] or SSH_OFF).strip().lower()
    except (IndexError, KeyError):
        return SSH_OFF          # 마이그레이션 전 행. 아무것도 열지 않는다.
    return v if v in SSH_LEVELS else SSH_OFF


def ssh_level(db, user):
    """그 사람에게 실제로 적용되는 등급. 정책 천장을 이미 적용한 값이다."""
    if not user:
        return SSH_OFF
    # 관리자는 이 표를 스스로 고칠 수 있다. 관리자에게만 못 쓰게 두는 것은
    # 연극이다. 대신 관리자가 한 일도 전부 기록에 남는다.
    mine = ssh_policy(db) if is_admin(user) else _stored_ssh_level(user)
    cap = ssh_policy(db)
    return mine if _SSH_RANK[mine] <= _SSH_RANK[cap] else cap


def ssh_can_read(db, user):
    return _SSH_RANK[ssh_level(db, user)] >= 1


def ssh_can_write(db, user):
    return _SSH_RANK[ssh_level(db, user)] >= 2


def ssh_all_servers(user):
    if not user:
        return False
    if is_admin(user):
        return True
    try:
        return bool(user["ssh_all_servers"])
    except (IndexError, KeyError):
        return False            # 마이그레이션 전 행. 기본은 '고른 것만' 이다.


def allowed_server_ids(db, user):
    """쓸 수 있는 서버 id 집합. None 이면 '전부' 라는 뜻이다."""
    if ssh_all_servers(user):
        return None
    rows = db.execute("SELECT server_id FROM ssh_grants WHERE user_id = ?",
                      (user["id"],)).fetchall()
    return {r["server_id"] for r in rows}


def visible_servers_clause(db, user, alias="s"):
    """
    서버 목록을 DB 단계에서 걸러낸다. 반환: (where 조각, 파라미터 list).

    화면에서 숨기는 것은 막은 것이 아니다. 목록 질의와 각 동작(터미널 열기,
    명령 실행, 대화에 붙이기) 양쪽에서 같은 규칙을 다시 본다.
    """
    if ssh_level(db, user) == SSH_OFF:
        return "0", []
    ids = allowed_server_ids(db, user)
    if ids is None:
        return "1", []
    if not ids:
        return "0", []
    marks = ",".join("?" for _ in ids)
    return "%s.id IN (%s)" % (alias, marks), sorted(ids)


def can_use_server(db, user, server_row):
    if server_row is None:
        return False
    if ssh_level(db, user) == SSH_OFF:
        return False
    ids = allowed_server_ids(db, user)
    return ids is None or server_row["id"] in ids


def require_server(db, user, server_row):
    """서버 하나를 쓸 수 있는지. 못 쓰면 403 이다. 404 로 숨기지 않는다.

    있는지 없는지를 숨겨 봐야 이름은 목록 어디서든 보인다. 대신 왜 막혔는지를
    말해 줘야 사용자가 관리자에게 무엇을 요청할지 안다.
    """
    if not user:
        abort(401, "로그인이 필요합니다.")
    if server_row is None:
        abort(404, "서버를 찾을 수 없습니다.")
    if ssh_level(db, user) == SSH_OFF:
        abort(403, "서버 사용이 허용되지 않았습니다. 관리자에게 요청하세요.")
    if not can_use_server(db, user, server_row):
        abort(403, "%s 서버는 사용 허용 범위에 없습니다. 관리자에게 요청하세요."
              % server_row["name"])


def set_user_ssh(db, user_id, level, all_servers, server_ids, granted_by=None):
    """
    한 사람의 등급과 범위를 통째로 교체한다. 반환: (등급, 남은 서버 id 목록)

    등급을 끄면 범위도 비운다. 꺼진 사람의 표에 서버가 남아 있으면 다음에 다시
    켤 때 예전 범위가 조용히 되살아난다.
    """
    from db import ts
    lv = (level or SSH_OFF).strip().lower()
    if lv not in SSH_LEVELS:
        lv = SSH_OFF
    all_flag = 1 if (all_servers and lv != SSH_OFF) else 0
    db.execute("UPDATE users SET ssh_level = ?, ssh_all_servers = ? WHERE id = ?",
               (lv, all_flag, user_id))
    db.execute("DELETE FROM ssh_grants WHERE user_id = ?", (user_id,))
    keep = []
    if lv != SSH_OFF and not all_flag:
        for sid in sorted({int(x) for x in server_ids}):
            if db.execute("SELECT 1 FROM ssh_servers WHERE id = ?", (sid,)).fetchone():
                keep.append(sid)
        if keep:
            db.executemany(
                "INSERT INTO ssh_grants (user_id, server_id, granted_at, granted_by)"
                " VALUES (?,?,?,?)",
                [(user_id, sid, ts(), granted_by) for sid in keep])
    return lv, keep
