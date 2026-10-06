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
     "min": 10, "max": 3600, "restart": False,
     "help": "이 값보다 gunicorn timeout 과 nginx proxy_read_timeout 이 커야 한다. "
             "기본 배포는 180 < 300 < 360 이므로 여기를 300 이상으로 올리려면 "
             "deploy/gunicorn.conf.py 와 nginx 설정도 함께 올릴 것"},
    {"key": "max_concurrent_claude", "label": "최대 동시 실행", "type": "int",
     "min": 1, "max": 32, "restart": False,
     "help": "서버 전체에서 동시에 실행할 Claude 요청 수. 세션 단위 lock 은 별도로 항상 동작"},

    # --- SSH 중계 (v8) ---------------------------------------------------
    # group 을 'relay' 로 둔다. Claude 설정 화면은 자기가 그릴 키만 집어 가므로
    # 여기 항목은 그 화면에 섞이지 않는다. 그리는 자리는 /admin/relay 다.
    {"key": "relay_policy", "label": "기본 정책", "type": "choice",
     "choices": ["read", "write"], "restart": False, "group": "relay",
     "help": "read = 조회만 허용. write = 변경도 허용하되 승인 카드를 받는다. "
             "사람마다 받은 등급보다 이 천장이 낮으면 천장이 이긴다"},
    {"key": "relay_poll_seconds", "label": "중계 대기 시간 (초)", "type": "int",
     "min": 5, "max": 60, "restart": False, "group": "relay",
     "help": "중계가 할 일을 기다리는 시간. gunicorn 스레드 하나를 그만큼 잡고 "
             "있으므로 늘릴 때는 deploy/gunicorn.conf.py 의 threads 를 함께 본다"},
    {"key": "relay_run_timeout", "label": "명령 하나 최대 (초)", "type": "int",
     "min": 5, "max": 120, "restart": False, "group": "relay",
     "help": "이 시간을 넘기면 중계가 끊고 실패로 올린다"},
    {"key": "relay_approval_seconds", "label": "승인 대기 (초)", "type": "int",
     "min": 30, "max": 600, "restart": False, "group": "relay",
     "help": "이 시간 안에 승인하지 않으면 취소된다. 승인 카드에 남은 시간을 보여 준다"},
    {"key": "relay_term_max_per_user", "label": "한 사람 동시 터미널", "type": "int",
     "min": 1, "max": 4, "restart": False, "group": "relay",
     "help": "워커 1개 / 스레드 8개로 도는 서버다. 이 값을 올리면 채팅이 느려진다"},
    {"key": "relay_term_idle_seconds", "label": "터미널 자동 닫기 (초)", "type": "int",
     "min": 30, "max": 1800, "restart": False, "group": "relay",
     "help": "브라우저가 이만큼 조용하면 그 터미널을 닫는다. 끊긴 세션을 서버 "
             "쪽에 남겨 두면 다음 사람이 붙지 못한다"},
    {"key": "relay_chat_max_commands", "label": "한 질문당 명령 수", "type": "int",
     "min": 1, "max": 5, "restart": False, "group": "relay",
     "help": "챗봇이 한 번의 질문에 고를 수 있는 명령 수. 넘으면 그만둔다"},
    {"key": "relay_tunnel_idle_seconds", "label": "PuTTY 터널 자동 닫기 (초)",
     "type": "int", "min": 60, "max": 3600, "restart": False, "group": "relay",
     "help": "터널에 이만큼 아무 바이트도 지나가지 않으면 닫는다. 심박은 세지 않는다. "
             "PuTTY 의 keepalive 를 켜 두면 그 바이트가 지나가므로 닫히지 않는다"},
    {"key": "relay_queue_keep_days", "label": "중계 큐 보관 (일)", "type": "int",
     "min": 7, "max": 365, "restart": False, "group": "relay",
     "help": "전송 큐(relay_jobs)의 끝난 행만 이 기간 뒤에 정리한다. "
             "기록(누가 언제 무엇을)은 지우지 않는다"},
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
# settings 밖에서 쓰는 암호화 (SSH 서버의 비밀번호)
#
# 키 파생 방법을 두 군데에 적어 두면 한쪽만 바뀌는 날 복호화가 조용히 실패한다.
# 그래서 저장 위치가 다른 값도 이 두 함수를 통해서만 암호화한다.
# ---------------------------------------------------------------------------
def encrypt_secret(value):
    return _encrypt(value)


def decrypt_secret(value):
    return _decrypt(value)


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
