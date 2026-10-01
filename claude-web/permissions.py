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
