#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
settings_store
==============

운영 중 바뀌는 설정(주로 Claude 연결)을 DB 의 settings 테이블에서 관리한다.

- 배포(.env)와 운영 설정(DB)을 분리하기 위한 계층이다.
- 값이 바뀌면 다음 요청부터 바로 반영된다. (provider 를 요청마다 스냅샷으로 생성)
- secret 타입 값은
    * 저장 시  : cryptography 가 있으면 SECRET_KEY 파생키로 암호화해서 넣는다.
                 없으면 평문으로 넣되 DB 파일 권한을 0600 으로 제한한다. (db.harden_permissions)
    * 조회 시  : 화면/API 로는 절대 원문을 내보내지 않고 마스킹된 미리보기만 준다.
    * 로그     : 어떤 경로로도 출력하지 않는다.
"""

import base64
import hashlib
import os
import threading
import time

from config import SECRET_KEY, SETTINGS_BOOTSTRAP
from db import ts

# ---------------------------------------------------------------------------
# 설정 정의
# ---------------------------------------------------------------------------
#   restart=True 인 항목은 관리자 화면에 "재시작 필요" 로 표시한다.
#   현재 Claude 관련 항목은 전부 즉시 반영된다.
SETTING_DEFS = [
    {"key": "claude_provider", "label": "Claude 연결 방식", "type": "choice",
     "choices": ["cli", "api"], "restart": False,
     "help": "cli = 서버에 설치된 Claude Code CLI 실행, api = Anthropic API 호출"},

    {"key": "claude_cli_path", "label": "Claude CLI 경로", "type": "text",
     "restart": False, "group": "cli",
     "help": "`which claude` 결과. 예: /usr/local/bin/claude"},
    {"key": "claude_workdir", "label": "Working Directory", "type": "text",
     "restart": False, "group": "cli", "must_be_dir": True,
     "help": "claude -p 를 실행할 디렉터리. 비우면 서버 프로세스의 작업 디렉터리"},
    {"key": "claude_extra_args", "label": "Extra Args", "type": "text",
     "restart": False, "group": "cli",
     "help": "claude 에 추가로 넘길 인자. 공백으로 구분. 예: --model sonnet"},
    {"key": "claude_use_resume", "label": "세션 resume 사용", "type": "bool",
     "restart": False, "group": "cli",
     "help": "끄면 매 요청마다 DB 의 최근 대화를 프롬프트에 넣는 방식으로 동작한다"},

    {"key": "claude_api_key", "label": "API Key", "type": "secret",
     "restart": False, "group": "api",
     "help": "저장 후에는 다시 표시되지 않는다. 비워서 저장하면 기존 값이 유지된다"},
    {"key": "claude_api_model", "label": "API 모델", "type": "text",
     "restart": False, "group": "api"},
    {"key": "claude_api_base_url", "label": "API Base URL", "type": "text",
     "restart": False, "group": "api"},

    {"key": "claude_timeout", "label": "Timeout (초)", "type": "int",
     "min": 10, "max": 3600, "restart": False},
    {"key": "max_concurrent_claude", "label": "최대 동시 실행", "type": "int",
     "min": 1, "max": 32, "restart": False,
     "help": "서버 전체에서 동시에 실행할 Claude 요청 수. 세션 단위 lock 은 별도로 항상 동작"},
]

DEFS_BY_KEY = {d["key"]: d for d in SETTING_DEFS}
SECRET_KEYS = {d["key"] for d in SETTING_DEFS if d["type"] == "secret"}

_ENC_PREFIX = "enc:v1:"
_CACHE_TTL = 3.0

_cache = {"at": 0.0, "values": None}
_lock = threading.RLock()


# ---------------------------------------------------------------------------
# secret 암호화 (cryptography 가 설치되어 있을 때만. 필수 의존성 아님)
# ---------------------------------------------------------------------------
def _fernet():
    try:
        from cryptography.fernet import Fernet
    except ImportError:
        return None
    digest = hashlib.pbkdf2_hmac(
        "sha256", SECRET_KEY.encode("utf-8"), b"claude-web-settings", 100000, dklen=32)
    return Fernet(base64.urlsafe_b64encode(digest))


def _encrypt(value):
    if not value:
        return ""
    f = _fernet()
    if f is None:
        return value
    return _ENC_PREFIX + f.encrypt(value.encode("utf-8")).decode("ascii")


def _decrypt(value):
    if not value or not value.startswith(_ENC_PREFIX):
        return value
    f = _fernet()
    if f is None:
        # 암호화해서 저장했는데 라이브러리가 사라진 경우. 원문을 알 수 없다.
        return ""
    try:
        return f.decrypt(value[len(_ENC_PREFIX):].encode("ascii")).decode("utf-8")
    except Exception:
        return ""


def mask(value):
    """화면에 보여줄 마스킹 문자열. 원문 길이 정보도 최소한만 흘린다."""
    if not value:
        return ""
    if len(value) <= 12:
        return "*" * 8
    return value[:7] + "*" * 8 + value[-4:]


def encryption_available():
    return _fernet() is not None


# ---------------------------------------------------------------------------
# 읽기
# ---------------------------------------------------------------------------
def bootstrap(db):
    """settings 에 없는 키만 .env 기반 초기값으로 채운다. (최초 1회)"""
    existing = {r["key"] for r in db.execute("SELECT key FROM settings")}
    now = ts()
    added = []
    for key, default in SETTINGS_BOOTSTRAP.items():
        if key in existing or key not in DEFS_BY_KEY:
            continue
        value = _encrypt(default) if key in SECRET_KEYS else default
        db.execute(
            "INSERT INTO settings (key, value, updated_at, updated_by) VALUES (?,?,?,NULL)",
            (key, value or "", now))
        added.append(key)
    if added:
        db.commit()
        invalidate()
    return added


def invalidate():
    with _lock:
        _cache["at"] = 0.0
        _cache["values"] = None


def load(db, force=False):
    """복호화된 원문 dict 를 돌려준다. 내부용."""
    with _lock:
        now = time.time()
        if not force and _cache["values"] is not None and now - _cache["at"] < _CACHE_TTL:
            return _cache["values"]
        values = dict(SETTINGS_BOOTSTRAP)
        for r in db.execute("SELECT key, value FROM settings"):
            values[r["key"]] = r["value"]
        for k in SECRET_KEYS:
            values[k] = _decrypt(values.get(k, ""))
        _cache["values"] = values
        _cache["at"] = now
        return values


def get(db, key, default=""):
    return load(db).get(key, default)


def get_int(db, key, default=0):
    try:
        return int(str(load(db).get(key, default)).strip())
    except (TypeError, ValueError):
        return default


def get_bool(db, key, default=True):
    v = str(load(db).get(key, "")).strip().lower()
    if v == "":
        return default
    return v not in ("0", "false", "no", "off")


def snapshot(db):
    """provider 가 쓸 설정 묶음. 원문 secret 을 포함하므로 밖으로 내보내지 않는다."""
    v = load(db)
    return {
        "provider": (v.get("claude_provider") or "cli").strip().lower(),
        "cli_path": (v.get("claude_cli_path") or "claude").strip(),
        "workdir": (v.get("claude_workdir") or "").strip() or None,
        "extra_args": (v.get("claude_extra_args") or "").split(),
        "use_resume": get_bool(db, "claude_use_resume", True),
        "timeout": max(10, get_int(db, "claude_timeout", 180)),
        "max_concurrent": max(1, get_int(db, "max_concurrent_claude", 3)),
        "api_key": v.get("claude_api_key") or "",
        "api_model": (v.get("claude_api_model") or "claude-sonnet-5").strip(),
        "api_base_url": (v.get("claude_api_base_url") or "https://api.anthropic.com").strip(),
    }


def public_view(db):
    """
    관리자 화면용. secret 은 원문 대신 preview/has_value 만 준다.
    """
    values = load(db)
    meta = {
        r["key"]: {"updated_at": r["updated_at"], "updated_by": r["updated_by"]}
        for r in db.execute("SELECT key, updated_at, updated_by FROM settings")
    }
    out = []
    for d in SETTING_DEFS:
        raw = values.get(d["key"], "")
        item = {
            "key": d["key"],
            "label": d["label"],
            "type": d["type"],
            "group": d.get("group", ""),
            "help": d.get("help", ""),
            "restart": d.get("restart", False),
            "choices": d.get("choices"),
            "min": d.get("min"),
            "max": d.get("max"),
            "updated_at": meta.get(d["key"], {}).get("updated_at"),
        }
        if d["type"] == "secret":
            item["value"] = ""
            item["preview"] = mask(raw)
            item["has_value"] = bool(raw)
        else:
            item["value"] = raw
        out.append(item)
    return out


# ---------------------------------------------------------------------------
# 쓰기
# ---------------------------------------------------------------------------
class SettingError(ValueError):
    pass


def _validate(d, raw):
    t = d["type"]
    if t == "choice":
        v = str(raw).strip().lower()
        if v not in d["choices"]:
            raise SettingError("%s: 허용되지 않는 값입니다." % d["label"])
        return v
    if t == "int":
        try:
            n = int(str(raw).strip())
        except (TypeError, ValueError):
            raise SettingError("%s: 숫자를 입력하세요." % d["label"])
        lo, hi = d.get("min"), d.get("max")
        if lo is not None and n < lo:
            raise SettingError("%s: 최소 %d 이상이어야 합니다." % (d["label"], lo))
        if hi is not None and n > hi:
            raise SettingError("%s: 최대 %d 이하여야 합니다." % (d["label"], hi))
        return str(n)
    if t == "bool":
        return "1" if str(raw).strip().lower() in ("1", "true", "yes", "on") else "0"
    v = str(raw or "").strip()
    if len(v) > 2000:
        raise SettingError("%s: 값이 너무 깁니다." % d["label"])
    # 오타 하나로 이후 모든 Claude 호출이 조용히 실패하는 것을 막는다
    if v and d.get("must_be_dir") and not os.path.isdir(v):
        raise SettingError("%s: 존재하는 디렉터리가 아닙니다: %s" % (d["label"], v))
    return v


def save(db, updates, user_id):
    """
    updates : {key: raw value}
    secret 은 빈 문자열이면 '변경하지 않음' 으로 본다. (화면에 원문이 없으므로)
    반환: 실제로 바뀐 key 목록 (값은 포함하지 않는다 - audit/로그 안전)
    """
    now = ts()
    current = load(db, force=True)
    changed = []
    for key, raw in updates.items():
        d = DEFS_BY_KEY.get(key)
        if d is None:
            continue
        if d["type"] == "secret":
            if not str(raw or "").strip():
                continue  # 빈 값 = 유지
            value = str(raw).strip()
            stored = _encrypt(value)
        else:
            value = _validate(d, raw)
            stored = value
        if current.get(key, "") == value:
            continue
        db.execute(
            "INSERT INTO settings (key, value, updated_at, updated_by) VALUES (?,?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
            "updated_at=excluded.updated_at, updated_by=excluded.updated_by",
            (key, stored, now, user_id))
        changed.append(key)
    if changed:
        db.commit()
        invalidate()
    return changed


def clear_secret(db, key, user_id):
    if key not in SECRET_KEYS:
        raise SettingError("secret 항목이 아닙니다.")
    db.execute(
        "INSERT INTO settings (key, value, updated_at, updated_by) VALUES (?,'',?,?) "
        "ON CONFLICT(key) DO UPDATE SET value='', updated_at=excluded.updated_at, "
        "updated_by=excluded.updated_by",
        (key, ts(), user_id))
    db.commit()
    invalidate()
