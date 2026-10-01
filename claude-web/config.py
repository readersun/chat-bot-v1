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
import sys

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

DOTENV_PATH = os.path.join(BASE_DIR, ".env")

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    pass
else:
    try:
        load_dotenv(DOTENV_PATH)
    except OSError as _exc:
        # 파일이 있는데 읽을 수 없는 경우(권한 등)에도 기동은 계속한다.
        #
        # Docker 배포에서는 compose 의 env_file 이 같은 값을 이미 환경변수로
        # 넣어주므로 이 파일을 못 읽어도 정상 동작한다. 컨테이너는 비특권
        # 사용자로 돌기 때문에 host 의 .env 가 root 전용(0600)이면 여기서
        # PermissionError 가 나서 예전에는 앱이 아예 기동하지 못했다.
        #
        # 다만 조용히 넘기면 SECRET_KEY 가 매번 임시값이 되는(=재시작마다 전원
        # 로그아웃) 사고를 눈치채지 못하므로, 경고는 반드시 남긴다.
        # 로거가 아직 없는 시점이라 stderr 로 쓴다. (systemd journal /
        # docker compose logs 에 그대로 보인다)
        # SECRET_KEY 가 이미 환경에 있으면 설정이 다른 경로로 들어온 것이다.
        # (Docker 배포의 정상 경로) 그때는 한 줄만 남겨 로그를 어지럽히지 않는다.
        if os.getenv("SECRET_KEY"):
            print("[config] %s 를 읽지 않았습니다 (%s). 환경변수 값을 사용합니다."
                  % (DOTENV_PATH, _exc), file=sys.stderr)
        else:
            for _line in (
                    "[config] 경고: %s 를 읽지 못했고 환경변수에도 설정이 없습니다 (%s)."
                    % (DOTENV_PATH, _exc),
                    "[config]        SECRET_KEY 가 임시값이 되어 재시작마다 전원 "
                    "로그아웃되고, DB/업로드 경로도 기본값으로 떨어집니다.",
                    "[config]        파일 권한을 확인하세요. 컨테이너 배포라면 "
                    "compose 의 env_file 설정을 확인하세요.",
            ):
                print(_line, file=sys.stderr)


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

# 메모 첨부파일. 기본값은 DB 와 같은 데이터 루트 아래이므로 Docker 배포에서
# 이미 마운트된 볼륨(/var/lib/claude-web)에 들어간다. compose 수정이 필요 없다.
NOTES_DIR = os.path.abspath(
    os.getenv("NOTES_DIR") or os.path.join(os.path.dirname(DATABASE_PATH), "notes"))

# 관리자 Storage 화면이 디스크 용량을 측정할 기준 경로.
# 컨테이너의 root filesystem 이 아니라 **실제 데이터가 저장되는** 파일시스템을
# 봐야 의미가 있으므로 기본값을 DB 가 있는 디렉터리로 잡는다.
STORAGE_MONITOR_PATH = os.path.abspath(
    os.getenv("STORAGE_MONITOR_PATH") or os.path.dirname(DATABASE_PATH))

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
# 메모 첨부파일
# ---------------------------------------------------------------------------
# 파일 하나당 최대 크기. 채팅 이미지(MAX_UPLOAD_MB)와 별도로 둔다.
MAX_NOTE_ATTACHMENT_MB = env_int("MAX_NOTE_ATTACHMENT_MB", 10)

# 한 번의 요청으로 올릴 수 있는 개수.
# 기본값 5 는 채팅(MAX_IMAGES_PER_MESSAGE)과 같게 맞춘 것이다. 덕분에 요청 본문
# 최대 크기가 기존과 같아 nginx 의 client_max_body_size(52m)를 바꿀 필요가 없다.
# 이 값이나 위 MB 를 올리면 nginx 설정도 함께 올려야 한다.
MAX_NOTE_ATTACHMENTS = env_int("MAX_NOTE_ATTACHMENTS", 5)

MAX_NOTE_TITLE_CHARS = env_int("MAX_NOTE_TITLE_CHARS", 200)
MAX_NOTE_CONTENT_CHARS = env_int("MAX_NOTE_CONTENT_CHARS", 50000)
MAX_NOTE_COMMENT_CHARS = env_int("MAX_NOTE_COMMENT_CHARS", 2000)

# 확장자 -> 허용 MIME. 하나의 확장자가 여러 MIME 을 가질 수 있어 tuple 이다.
# 실행 파일 / script / shell 은 넣지 않는다. 확장자만 믿지 않고 내용도 검사한다.
# (notes.py 의 sniff_note_file)
ALLOWED_NOTE_FILES = {
    "png":  ("image/png",),
    "jpg":  ("image/jpeg",),
    "jpeg": ("image/jpeg",),
    "webp": ("image/webp",),
    "gif":  ("image/gif",),
    "pdf":  ("application/pdf",),
    "txt":  ("text/plain",),
}

# ---------------------------------------------------------------------------
# 관리자 Storage 화면
# ---------------------------------------------------------------------------
# 디렉터리 용량 계산 결과를 이 초 동안 재사용한다. 파일이 많아지면 매 요청마다
# 전체를 순회하는 비용이 커지므로 짧게 캐시한다. (Redis 같은 외부 저장소는 쓰지 않는다)
# 화면의 [새로고침] 은 캐시를 무시하고 다시 계산한다.
STORAGE_CACHE_SECONDS = env_int("STORAGE_CACHE_SECONDS", 60)

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
