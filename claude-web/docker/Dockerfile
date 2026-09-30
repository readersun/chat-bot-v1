# syntax=docker/dockerfile:1
#
# claude-web *실행환경* 이미지
# ===========================
#
# 이 이미지에는 "환경" 만 들어간다.
#
#     Python 3.12 / requirements.txt 의 pip 패키지 / gunicorn / Claude Code CLI
#
# 웹 애플리케이션 소스(app.py, templates/, static/ ...)는 **넣지 않는다.**
# 운영 서버의 git 작업 트리를 /app 으로 bind mount 해서 쓴다.
#
#     소스만 변경            -> git pull + docker compose restart app  (재빌드 없음)
#     requirements.txt 변경  -> docker compose build app               (재빌드 필요)
#
# 빌드:
#     docker compose build app
#   또는
#     docker build -f docker/Dockerfile -t claude-web-runtime:1.0.0 .
#
# ---------------------------------------------------------------------------

ARG PYTHON_IMAGE=python:3.12-slim-trixie
FROM ${PYTHON_IMAGE}

# 컨테이너 사용자. bind mount 한 /var/lib/claude-web 의 host 소유자와 맞추기 위해
# UID/GID 를 빌드 인자로 뺐다. 맞추지 않아도 entrypoint 가 chown 으로 정리한다.
ARG APP_UID=1000
ARG APP_GID=1000
# Claude Code apt 저장소 채널. stable = 약 1주 지연된 검증 버전, latest = 최신 릴리스
ARG CLAUDE_CODE_CHANNEL=stable

ENV APP_USER=claudeweb \
    APP_HOME=/home/claudeweb \
    DATA_ROOT=/var/lib/claude-web \
    LANG=C.UTF-8 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONIOENCODING=utf-8 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_ROOT_USER_ACTION=ignore

# ---------------------------------------------------------------------------
# 1) OS 패키지 + Claude Code CLI
#
# 넣는 것
#   ca-certificates  api.anthropic.com 으로 나가는 TLS 검증
#   curl             Claude 서명키 다운로드 / compose healthcheck / 디버깅
#   gnupg            서명키 지문 확인용. 확인이 끝나면 지운다 (런타임에 불필요)
#   claude-code      Claude Code CLI. 공식 apt 저장소에서 설치한다.
#
# 넣지 않는 것
#   git              git pull 은 **host** 에서 한다. 컨테이너는 소스를 읽기만 한다.
#   ripgrep          Claude Code 에 포함되어 있다.
#   build 도구       requirements.txt 의 패키지는 전부 manylinux wheel 이 있다.
#   node/npm         apt 패키지는 네이티브 바이너리라서 Node 런타임이 필요 없다.
#
# 서명키 지문은 공식 문서(code.claude.com/docs/en/setup)에 공개된 값이다.
# 다르면 grep 이 실패하고 이미지 빌드가 그 자리에서 멈춘다.
#
# apt 로 설치한 Claude Code 는 스스로 자동 업데이트하지 않는다. 그래서 이미지
# 태그가 곧 Claude CLI 버전이 된다. (원할 때 재빌드해서 올린다)
# ---------------------------------------------------------------------------
RUN set -eux; \
    apt-get update; \
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        ca-certificates curl gnupg; \
    install -d -m 0755 /etc/apt/keyrings; \
    curl -fsSL https://downloads.claude.ai/keys/claude-code.asc \
        -o /etc/apt/keyrings/claude-code.asc; \
    gpg --show-keys --with-colons /etc/apt/keyrings/claude-code.asc \
        | grep -q '^fpr:::::::::31DDDE24DDFAB679F42D7BD2BAA929FF1A7ECACE:'; \
    printf 'deb [signed-by=%s] %s %s main\n' \
        /etc/apt/keyrings/claude-code.asc \
        "https://downloads.claude.ai/claude-code/apt/${CLAUDE_CODE_CHANNEL}" \
        "${CLAUDE_CODE_CHANNEL}" > /etc/apt/sources.list.d/claude-code.list; \
    apt-get update; \
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends claude-code; \
    apt-get purge -y --auto-remove gnupg; \
    rm -rf /var/lib/apt/lists/*; \
    command -v claude; \
    claude --version

# ---------------------------------------------------------------------------
# 2) Python 의존성
#
# 소스 전체를 COPY 하지 않는다. dependency manifest 하나만 COPY 한다.
#   - app.py / templates / static 을 고쳐도 이 레이어는 캐시에서 재사용된다.
#   - requirements.txt 를 고치면 이 레이어부터 다시 빌드된다.
# ---------------------------------------------------------------------------
COPY requirements.txt /tmp/requirements.txt
RUN set -eux; \
    python -m pip install --upgrade pip; \
    python -m pip install -r /tmp/requirements.txt; \
    rm -f /tmp/requirements.txt; \
    gunicorn --version; \
    python -c "from importlib.metadata import version; import flask, werkzeug, dotenv, cryptography; print('installed:', {n: version(n) for n in ('flask','werkzeug','python-dotenv','cryptography','gunicorn')})"

# ---------------------------------------------------------------------------
# 3) 실행 사용자
#
# /bin/bash  : 최초 Claude 로그인(`docker compose exec app claude`) 에 셸이 필요하다.
# ~/.claude  : Claude 인증정보(.credentials.json, 0600) 가 생기는 곳.
#              compose 의 named volume 으로 덮어써서 컨테이너를 지워도 남긴다.
# ---------------------------------------------------------------------------
RUN set -eux; \
    groupadd -g "${APP_GID}" "${APP_USER}"; \
    useradd -u "${APP_UID}" -g "${APP_GID}" -m -d "${APP_HOME}" -s /bin/bash "${APP_USER}"; \
    install -d -m 0700 -o "${APP_USER}" -g "${APP_USER}" "${APP_HOME}/.claude"; \
    install -d -m 0750 -o "${APP_USER}" -g "${APP_USER}" "${DATA_ROOT}"

# HOME 은 사용자를 만든 *뒤에* 세팅한다. 먼저 세팅하면 빌드 중 gpg 가 아직
# 존재하지 않는 홈 디렉터리에 .gnupg 를 만들려다 실패한다.
# 런타임에는 Claude CLI 가 이 값으로 인증정보 위치(~/.claude)를 정한다.
ENV HOME=/home/claudeweb

# Claude CLI 의 백그라운드 자동 업데이트를 끈다.
#
# apt 로 설치한 Claude Code 는 원래 자동 업데이트하지 않지만, 명시적으로 꺼서
# "이미지 태그 = Claude CLI 버전" 이 항상 성립하게 만든다. 자동 업데이트가 돌면
# 새 버전이 HOME 볼륨(~/.local/bin)에 깔려 이미지보다 우선하게 되고, 같은 태그의
# 이미지가 서버마다 다르게 동작한다. Claude CLI 를 올리려면 이미지를 재빌드한다.
ENV DISABLE_AUTOUPDATER=1

COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod 0755 /usr/local/bin/entrypoint.sh

# 소스는 이미지에 없다. 운영에서 여기에 host 의 git 작업 트리를 마운트한다.
WORKDIR /app

EXPOSE 8080

# entrypoint 는 root 로 시작해 데이터 디렉터리 소유권만 정리한 뒤 곧바로
# claudeweb 으로 내려가서 CMD 를 exec 한다. (웹/Claude 는 root 로 돌지 않는다)
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]

# gunicorn 설정도 마운트된 소스에 있다 -> 워커/타임아웃 조정에 재빌드가 필요 없다.
CMD ["gunicorn", "--config", "/app/deploy/gunicorn.conf.py", "app:app"]
