#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
config
======

**서버 자체**에 필요한 고정 설정만 담는다. (.env / 환경변수)

운영 중 바뀔 수 있는 Claude 관련 설정은 여기가 아니라 DB 의 settings 테이블에서
관리한다. (settings_store.py / 관리자 페이지)

다만 최초 기동 때 settings 테이블이 비어 있으면 아래 CLAUDE_* 값을 초기값으로
한 번 심어준다. 덕분에 기존 .env 로 운영하던 서버도 그대로 올라오고, 이후에는
관리자 페이지에서 바꾸면 된다.
"""

import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

try:
    from dotenv import load_dotenv

    load_dotenv(os.path.join(BASE_DIR, ".env"))
except ImportError:  # pragma: no cover
    pass


def env_int(name, default):
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def env_bool(name, default=True):
    v = os.getenv(name)
    if v is None:
        return default
    return v.strip().lower() not in ("0", "false", "no", "off", "")


# ---------------------------------------------------------------------------
# 저장 위치 / 서버
# ---------------------------------------------------------------------------
DATABASE_PATH = os.path.abspath(
    os.getenv("DATABASE_PATH") or os.path.join(BASE_DIR, "data", "chat.db"))
UPLOAD_DIR = os.path.abspath(
    os.getenv("UPLOAD_DIR") or os.path.join(BASE_DIR, "data", "uploads"))
BACKUP_DIR = os.path.abspath(
    os.getenv("BACKUP_DIR") or os.path.join(os.path.dirname(DATABASE_PATH), "backups"))

HOST = os.getenv("HOST", "0.0.0.0")
PORT = env_int("PORT", 8080)

SECRET_KEY = os.getenv("SECRET_KEY") or ""
SECRET_KEY_IS_EPHEMERAL = not SECRET_KEY
if not SECRET_KEY:
    # .env 에 없으면 프로세스마다 랜덤 -> 재시작 시 전원 로그아웃된다.
    # 운영에서는 반드시 .env 에 고정값을 넣어야 한다. (관리자 화면에서 경고)
    SECRET_KEY = os.urandom(32).hex()

# ---------------------------------------------------------------------------
# 쿠키 / 세션
# ---------------------------------------------------------------------------
# HTTPS 로 서비스한다면 1 을 권장한다. 평문 HTTP 에서 1 로 두면 브라우저가
# 쿠키를 저장하지 않아 로그인이 되지 않으므로 기본값은 0 이다.
SESSION_COOKIE_SECURE = env_bool("SESSION_COOKIE_SECURE", False)
SESSION_LIFETIME_DAYS = env_int("SESSION_LIFETIME_DAYS", 14)

# nginx 등 리버스 프록시 뒤에 둘 때만 1 로 둔다. 로그인 rate limit 이 클라이언트
# IP 를 X-Forwarded-For 에서 읽는다. 프록시가 없는데 켜면 IP 위조가 가능해진다.
TRUST_PROXY = env_bool("TRUST_PROXY", False)

# ---------------------------------------------------------------------------
# 업로드 / 입력 제한
# ---------------------------------------------------------------------------
MAX_UPLOAD_MB = env_int("MAX_UPLOAD_MB", 10)
MAX_IMAGES_PER_MESSAGE = env_int("MAX_IMAGES_PER_MESSAGE", 5)
MAX_INPUT_CHARS = env_int("MAX_INPUT_CHARS", 8000)
MAX_HISTORY_MESSAGES = env_int("MAX_HISTORY_MESSAGES", 16)
MAX_HISTORY_CHARS = env_int("MAX_HISTORY_CHARS", 12000)

ALLOWED_IMAGES = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "webp": "image/webp",
    "gif": "image/gif",
}

# ---------------------------------------------------------------------------
# 로그인 정책
# ---------------------------------------------------------------------------
MIN_PASSWORD_LENGTH = env_int("MIN_PASSWORD_LENGTH", 8)
LOGIN_MAX_FAILURES = env_int("LOGIN_MAX_FAILURES", 5)
LOGIN_WINDOW_MINUTES = env_int("LOGIN_WINDOW_MINUTES", 10)
# IP 기준 한도는 계정 기준보다 훨씬 느슨하게 둔다.
# 사내에서는 NAT 나 리버스 프록시 때문에 여러 사람이 같은 IP 로 보인다.
# 여기를 계정과 같은 값으로 두면 한 사람의 오타 몇 번으로 사무실 전체가 잠긴다.
LOGIN_IP_FACTOR = env_int("LOGIN_IP_FACTOR", 4)

# ---------------------------------------------------------------------------
# settings 테이블 초기값 (최초 1회만 사용. 이후에는 관리자 페이지에서 변경)
# ---------------------------------------------------------------------------
SETTINGS_BOOTSTRAP = {
    "claude_provider": os.getenv("CLAUDE_PROVIDER", "cli"),
    "claude_cli_path": os.getenv("CLAUDE_BIN", "claude"),
    "claude_workdir": os.getenv("CLAUDE_WORKDIR", ""),
    "claude_timeout": str(env_int("CLAUDE_TIMEOUT", 180)),
    "claude_extra_args": os.getenv("CLAUDE_EXTRA_ARGS", ""),
    "claude_use_resume": "1" if env_bool("CLAUDE_USE_RESUME", True) else "0",
    "max_concurrent_claude": str(
        env_int("MAX_CONCURRENT_CLAUDE", env_int("MAX_CONCURRENCY", 3))),
    "claude_api_key": os.getenv("CLAUDE_API_KEY", ""),
    "claude_api_model": os.getenv("CLAUDE_API_MODEL", "claude-sonnet-5"),
    "claude_api_base_url": os.getenv("CLAUDE_API_BASE_URL", "https://api.anthropic.com"),
}
