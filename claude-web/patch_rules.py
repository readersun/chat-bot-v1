#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
patch_rules
===========

패치 저장소의 "읽는 규칙"만 모은 파일. DB 도 Flask 도 파일시스템도 건드리지
않는다. 순수 함수뿐이라 혼자 돌려 보며 고칠 수 있다.

    /{와칭 루트}/{사이트}/{제품 라인}/{날짜}/{모듈}_{버전}*.tar

여기서 다루는 것은 뒤의 셋이다.

    날짜 폴더   260923 / 20260923 / 2026-09-23 ...   -> parse_date_dir
    모듈+버전   face_1.2.14.tar                       -> match_file
    정렬 키     1.2.14 가 1.2.9 보다 높다              -> version_sort_key

틀리기 쉬운 지점 셋 (전부 실제 데이터에서 나왔다)
--------------------------------------------------
1. 모듈 이름에 밑줄이 들어간다. `stt_unified`, `scene_gateway`.
   밑줄로 자르면 깨진다. 그래서 **등록된 이름을 긴 것부터** 맞춰 본다.
2. `1.2.14` 는 `1.2.9` 보다 높다. 문자열로 정렬하면 거꾸로 간다.
   그래서 숫자 묶음을 0 으로 채운 정렬 키를 따로 만든다.
3. `260923` 이 YYMMDD 인지 DDMMYY 인지는 폴더 이름만 봐서는 알 수 없다.
   짐작하지 않고 설정으로 받는다.
"""

import datetime
import re

# ---------------------------------------------------------------------------
# 경로 조각
# ---------------------------------------------------------------------------
# 스캔이 디렉터리 이름을 DB 에 넣기 전에 통과시키는 검사. DAS 에 이런 이름이
# 생길 일은 없지만, 생겼다면 그게 바로 공격이다.
_BAD_SEGMENT_CHARS = ("/", "\\", "\x00")


def safe_segment(name):
    """경로 한 칸으로 써도 되는 이름인가."""
    if not name or len(name) > 255:
        return False
    if name in (".", ".."):
        return False
    if name.startswith("."):          # 숨김 폴더와 rsync 임시파일
        return False
    return not any(c in name for c in _BAD_SEGMENT_CHARS)


# ---------------------------------------------------------------------------
# 날짜 폴더 형식
# ---------------------------------------------------------------------------
# 관리자 화면의 선택 목록. 여기에 없는 꼴은 직접 적는다.
# 날짜 폴더 형식
# ----------------------------------------------------------------------------
# YYYY YY MM DD 는 숫자 자리, * 는 아무 글자나(없어도 된다), 그 밖의 글자는
# 글자 그대로다. 그래서 이런 폴더 이름을 다 받을 수 있다.
#
#     260923                YYMMDD
#     260923_prod           YYMMDD_prod    (글자를 그대로 적는다)
#     260923 과 260923_prod YYMMDD*        (섞여 있으면 * 로 받는다)
#     rel_20261005          rel_YYYYMMDD
#
# 꼬리표가 폴더마다 다르면(_prod, _hotfix, -rc1) * 쪽이 맞는다. 형식은 사이트
# 하나에 하나만 고를 수 있어서, 글자를 그대로 적으면 꼬리표 없는 폴더가 빠진다.
DATE_PRESETS = ("YYMMDD", "YYMMDD*", "YYYYMMDD", "YYYYMMDD*",
                "YYYY-MM-DD", "YYYY-MM-DD*", "YY-MM-DD")
DEFAULT_DATE_FORMAT = "YYMMDD"

MAX_DATE_FORMAT_LEN = 64
# * 를 몇 개까지 허용할까. 여러 개 쓸 이유가 없고, .* 가 늘어나면 폴더 이름
# 하나를 맞추는 데 드는 되돌림이 커진다.
MAX_DATE_WILDCARDS = 3

# 긴 토큰을 먼저 본다. YYYY 를 YY 두 개로 읽으면 안 된다.
_DATE_TOKENS = (
    ("YYYY", r"(?P<Y4>\d{4})"),
    ("YY", r"(?P<Y2>\d{2})"),
    ("MM", r"(?P<M>\d{2})"),
    ("DD", r"(?P<D>\d{2})"),
    # 아무 글자나. 없어도 맞는다. 숫자를 뺀 글자로 좁히고 싶었지만 _v2 처럼
    # 숫자가 섞인 꼬리표도 있어서 열어 둔다.
    ("*", r".*"),
)


def validate_date_format(fmt):
    """(ok, 오류문장). 저장 전에 통과해야 한다."""
    fmt = str(fmt or "")
    if not fmt:
        return False, "날짜 형식을 입력해 주세요."
    if len(fmt) > MAX_DATE_FORMAT_LEN:
        return False, "날짜 형식이 너무 깁니다. (최대 %d자)" % MAX_DATE_FORMAT_LEN

    has_y4 = "YYYY" in fmt
    # YYYY 를 걷어낸 뒤에 YY 가 남아 있는지 본다
    rest = fmt.replace("YYYY", "")
    has_y2 = "YY" in rest
    if has_y4 and has_y2:
        return False, "YYYY 와 YY 는 함께 쓸 수 없습니다. 하나만 넣어 주세요."
    if not has_y4 and not has_y2:
        return False, "연도 자리가 없습니다. YYYY 또는 YY 를 넣어 주세요."
    if "MM" not in fmt:
        return False, "월 자리가 없습니다. MM 을 넣어 주세요."
    if "DD" not in fmt:
        return False, "일 자리가 없습니다. DD 를 넣어 주세요."
    if fmt.count("*") > MAX_DATE_WILDCARDS:
        return False, "* 는 %d개까지만 쓸 수 있습니다." % MAX_DATE_WILDCARDS
    return True, ""


def compile_date_format(fmt):
    """
    형식 문자열을 정규식으로 바꾼다. 토큰이 아닌 글자는 그대로 있어야 한다.

    * 가 날짜 앞에 오면 앞쪽이 욕심껏 먹는다. 즉 날짜로 읽을 수 있는 자리가
    여럿일 때 **뒤쪽**을 날짜로 본다. (20260923 을 *YYMMDD 로 읽으면 20 을
    버리고 260923 을 날짜로 본다) 꼬리표는 뒤에 붙는 것이 보통이므로 * 는
    끝에 쓰는 쪽을 권한다.
    """
    ok, _ = validate_date_format(fmt)
    if not ok:
        return None

    out, i = [], 0
    while i < len(fmt):
        for token, piece in _DATE_TOKENS:
            if fmt.startswith(token, i):
                out.append(piece)
                i += len(token)
                break
        else:
            out.append(re.escape(fmt[i]))
            i += 1
    try:
        return re.compile("^" + "".join(out) + "$")
    except re.error:
        return None


def parse_date_dir(name, fmt):
    """
    폴더 이름을 'YYYY-MM-DD' 로 돌려준다. 못 읽으면 None.

    None 은 "버린다"는 뜻이 아니다. 호출하는 쪽은 폴더 이름을 그대로 보관하고
    날짜만 비워 둔다. 형식을 잘못 적은 날 목록이 통째로 비면 안 된다.
    """
    rx = compile_date_format(fmt)
    if rx is None:
        return None
    m = rx.match(str(name or ""))
    if not m:
        return None

    g = m.groupdict()
    # YY 는 2000~2099 로 읽는다. 1900 년대 패치 폴더가 있을 리 없다.
    year = int(g["Y4"]) if g.get("Y4") else 2000 + int(g["Y2"])
    try:
        return datetime.date(year, int(g["M"]), int(g["D"])).isoformat()
    except ValueError:
        return None      # 2026-02-31 같은 날짜


# ---------------------------------------------------------------------------
# 버전 규칙
# ---------------------------------------------------------------------------
# 마이그레이션이 넣어 두는 내장 규칙. 관리자가 지우거나 고칠 수 있다.
BUILTIN_VERSION_RULES = (
    {"name": "semver3", "pattern": r"^(\d+)\.(\d+)\.(\d+)$",
     "sort_kind": "numeric", "sample": "1.2.14"},
    {"name": "semver4", "pattern": r"^(\d+)\.(\d+)\.(\d+)\.(\d+)$",
     "sort_kind": "numeric", "sample": "1.2.14.3"},
    {"name": "date8", "pattern": r"^(\d{8})$",
     "sort_kind": "numeric", "sample": "20260923"},
    {"name": "numeric", "pattern": r"^(\d+(?:\.\d+)*)$",
     "sort_kind": "numeric", "sample": "1.2.14"},
)
DEFAULT_VERSION_RULE = "semver3"
SORT_KINDS = ("numeric", "lexical")

MAX_PATTERN_LEN = 200

# 버전 뒤에 가 붙는 것. 규칙의 `*` 자리다. face_1.2.14_hotfix.tar -> hotfix
_SUFFIX = r"(?:[._-](?P<suffix>.+))?"

# 스캔을 멈춰 세우는 중첩 수량자. 파이썬 re 에는 타임아웃이 없어서 길이 제한과
# 이 검사가 유일한 방어다.
#
# 잡아야 하는 것은 "반복 가능한 조각 하나"를 다시 반복시킨 꼴이다.
#     (\d+)+   (a*)*   ([abc]+)*   (\w+)+
# 이런 식은 같은 글자를 나누는 경우의 수가 폭발해 입력 몇십 자에 멈춰 버린다.
#
# 반대로 아래는 안전하고, 내장 규칙 numeric 이 실제로 이 꼴이다.
#     (?:\.\d+)*     반복마다 '.' 를 반드시 먹어서 나눌 여지가 없다
# 그래서 "그룹 전체에 수량자가 붙었다"만으로 막으면 멀쩡한 규칙까지 막는다.
_QUANTIFIED_GROUP = re.compile(r"\((\?[:=!]|\?<[=!])?([^()]*)\)[+*]")
_LONE_REPEATED_ATOM = re.compile(r"^(?:\\[dDwWsS]|\[[^\]]+\]|[^\\\[\](){}|^$.+*?])[+*]$")


def _has_nested_quantifier(pattern):
    for m in _QUANTIFIED_GROUP.finditer(pattern):
        if _LONE_REPEATED_ATOM.match(m.group(2) or ""):
            return True
    return False


def validate_version_pattern(pattern, sample=""):
    """(ok, 오류문장). 샘플을 주면 그 샘플이 실제로 잡히는지까지 본다."""
    pattern = str(pattern or "")
    if not pattern:
        return False, "정규식을 입력해 주세요."
    if len(pattern) > MAX_PATTERN_LEN:
        return False, "정규식이 너무 깁니다. (최대 %d자)" % MAX_PATTERN_LEN
    if _has_nested_quantifier(pattern):
        return False, "중첩 수량자는 스캔을 멈춰 세울 수 있어 쓸 수 없습니다. 예: (\\d+)+"
    try:
        compile_version_rule(pattern)
    except re.error as exc:
        return False, "정규식을 읽을 수 없습니다: %s" % exc

    if sample:
        if parse_version(sample, pattern) is None:
            return False, "샘플 '%s' 이(가) 이 규칙에 맞지 않습니다." % sample
    return True, ""


def compile_version_rule(pattern):
    """
    저장된 정규식은 '버전 하나'만 가리키는 full match 식이다.
    여기서 앞뒤 앵커를 떼고 접미사 자리를 붙여 다시 묶는다.
    """
    body = str(pattern or "")
    if body.startswith("^"):
        body = body[1:]
    if body.endswith("$") and not body.endswith(r"\$"):
        body = body[:-1]
    return re.compile("^(?P<version>" + body + ")" + _SUFFIX + "$")


def parse_version(rest, pattern):
    """'1.2.14' 또는 '1.2.14_hotfix' -> (버전, 접미사). 안 맞으면 None."""
    try:
        rx = compile_version_rule(pattern)
    except re.error:
        return None
    m = rx.match(str(rest or ""))
    if not m:
        return None
    return m.group("version"), (m.group("suffix") or "")


_DIGITS = re.compile(r"(\d+)")


def version_sort_key(version, sort_kind="numeric"):
    """
    ORDER BY 에 쓸 문자열. 숫자 묶음을 10자리로 0 을 채운다.

        1.2.14 -> 0000000001.0000000002.0000000014
        1.2.9  -> 0000000001.0000000002.0000000009

    이게 이 파일에서 제일 중요한 함수다. 이걸 빼먹으면 화면이 1.2.9 를 최신으로
    보여 주고, 현장은 구버전을 받아 간다.
    """
    text = str(version or "")
    if sort_kind == "lexical":
        return text.lower()
    out = []
    for piece in _DIGITS.split(text):
        if not piece:
            continue
        out.append(piece.rjust(10, "0") if piece.isdigit() else piece.lower())
    return "".join(out)


# ---------------------------------------------------------------------------
# 파일 하나 읽기
# ---------------------------------------------------------------------------
TAR_SUFFIX = ".tar"

# 복사가 끝나지 않은 파일. 이름만으로 거르는 1차 방어다.
# (나이 제한과 크기 안정 확인은 스캐너가 따로 한다)
_TEMP_NAMES = re.compile(r"(\.tmp|\.part|\.filepart|\.swp|~)$", re.IGNORECASE)


def is_patch_file(filename):
    """*.tar 인가. 임시 파일과 숨김 파일은 뺀다."""
    name = str(filename or "")
    if not name or name.startswith("."):
        return False
    if _TEMP_NAMES.search(name):
        return False
    return name.lower().endswith(TAR_SUFFIX)


def match_file(filename, modules):
    """
    파일 이름 하나를 등록된 모듈 목록에 맞춰 본다.

        modules : [{"id":…, "name":"face", "pattern":"^(\\d+)\\.(\\d+)\\.(\\d+)$",
                    "sort_kind":"numeric"}, …]
                  pattern 은 모듈 규칙이 없으면 제품 라인 기본값을 넣어 준다.

    돌려주는 값
        {"module": <그 dict>, "version": "1.2.14", "suffix": "hotfix",
         "version_sort": "…"}   또는 None (= 매칭 안 된 파일)

    긴 이름부터 보는 이유: stt 와 stt_unified 가 함께 등록돼 있을 때 짧은 쪽을
    먼저 집으면 stt_unified_1.0.0.tar 의 버전이 'unified_1.0.0' 이 된다.
    """
    if not is_patch_file(filename):
        return None
    stem = filename[: -len(TAR_SUFFIX)]

    candidates = [m for m in modules
                  if m.get("name") and stem.startswith(m["name"] + "_")]
    candidates.sort(key=lambda m: len(m["name"]), reverse=True)

    for mod in candidates:
        rest = stem[len(mod["name"]) + 1:]
        parsed = parse_version(rest, mod.get("pattern") or "")
        if parsed is None:
            continue
        version, suffix = parsed
        return {
            "module": mod,
            "version": version,
            "suffix": suffix,
            "version_sort": version_sort_key(version, mod.get("sort_kind") or "numeric"),
        }
    return None


def candidate_module_name(filename):
    """
    매칭 안 된 tar 에서 모듈 이름 후보를 뽑는다. 관리자 화면이 등록 칸에 미리
    채워 주는 값이고, 그대로 등록되지는 않는다. 사람이 보고 고친다.

        nlp_engine_1.0.0.tar -> nlp_engine
    """
    if not is_patch_file(filename):
        return ""
    stem = filename[: -len(TAR_SUFFIX)]
    # 뒤에서부터 "숫자로 시작하는 조각"을 만나면 거기서 자른다
    parts = stem.split("_")
    while len(parts) > 1 and parts[-1] and parts[-1][0].isdigit():
        parts.pop()
    return "_".join(parts) if len(parts) >= 1 else stem
