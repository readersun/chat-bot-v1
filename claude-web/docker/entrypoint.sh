#!/bin/sh
#
# claude-web 컨테이너 entrypoint
#
# root 로 실행되면 (compose 의 init 서비스)
#     1) 데이터 디렉터리 생성
#     2) 소유권을 claudeweb 으로 정리
#     3) 그 뒤 claudeweb 으로 내려가서 인자를 exec
#
# 이미 claudeweb 으로 실행되면 (compose 의 app 서비스)
#     점검/경고만 출력하고 바로 exec
#
# app 서비스를 root 로 띄우지 않는 이유
# ------------------------------------
# `docker compose exec` 는 entrypoint 를 거치지 않고 컨테이너에 설정된
# 사용자로 실행된다. app 을 root 로 띄우면
#
#     docker compose exec app claude        -> root 가 ~/.claude/.credentials.json 을
#                                              0600 root 소유로 만든다
#                                              -> 웹(claudeweb)이 못 읽어서 인증 실패
#     docker compose exec app python app.py create-admin
#                                           -> root 소유 chat.db-wal 이 생겨서
#                                              웹이 DB 에 못 쓴다
#
# 그래서 권한 정리는 별도의 init 서비스(root, 1회 실행)가 하고, app 은 처음부터
# claudeweb 으로 돈다. 덕분에 `docker compose exec app ...` 가 기본적으로 안전하다.
#
# 마이그레이션은 여기서 하지 않는다. app.py 를 import 하는 순간
# startup() -> migrate() 가 돌기 때문에 gunicorn 기동 시 자동으로 수행된다.
#
set -eu

APP_USER="${APP_USER:-claudeweb}"
APP_HOME="${APP_HOME:-/home/claudeweb}"
DATA_ROOT="${DATA_ROOT:-/var/lib/claude-web}"

log() { printf '[entrypoint] %s\n' "$*" >&2; }

# --- 항상 하는 점검 ---------------------------------------------------------
# 데이터 경로가 마운트되어 있으면 / 와 다른 device 위에 있다. 같다면 컨테이너
# 레이어라서 `docker compose down` 한 번에 DB 와 업로드가 사라진다.
if [ "$(stat -c %d / 2>/dev/null)" = "$(stat -c %d "$DATA_ROOT" 2>/dev/null)" ]; then
    log "############################################################"
    log "경고: $DATA_ROOT 이 볼륨으로 마운트되어 있지 않습니다."
    log "      이대로 쓰면 컨테이너를 재생성할 때 DB 와 업로드가 사라집니다."
    log "      compose.yml 의 volumes 설정을 확인하세요."
    log "############################################################"
fi

if [ ! -f /app/app.py ]; then
    log "경고: /app/app.py 가 없습니다. 소스 디렉터리를 /app 에 마운트했는지 확인하세요."
fi

# --- root 일 때만: 디렉터리/권한 정리 ---------------------------------------
if [ "$(id -u)" = "0" ]; then
    uid="$(id -u "$APP_USER")"
    gid="$(id -g "$APP_USER")"

    # DB / 업로드 / Claude working dir / 마이그레이션 백업.
    # .env 의 DATABASE_PATH, UPLOAD_DIR, BACKUP_DIR, CLAUDE_WORKDIR 이 여기를 본다.
    # workspace 는 관리자 화면이 "존재하는 디렉터리" 인지 검사하므로 미리 만든다.
    for d in "$DATA_ROOT" "$DATA_ROOT/uploads" "$DATA_ROOT/workspace" "$DATA_ROOT/backups"; do
        if [ ! -d "$d" ]; then
            log "mkdir $d"
            mkdir -p "$d"
        fi
    done
    chmod 0750 "$DATA_ROOT" 2>/dev/null || true

    # 소유자가 틀린 파일이 하나라도 있을 때만 재귀 chown 한다.
    # (매번 chown -R 하면 업로드가 쌓일수록 기동이 느려진다)
    for root in "$DATA_ROOT" "$APP_HOME"; do
        if find "$root" \( ! -user "$APP_USER" -o ! -group "$APP_USER" \) -print -quit \
                2>/dev/null | grep -q .; then
            log "chown -R ${uid}:${gid} $root"
            chown -R "${uid}:${gid}" "$root"
        fi
    done

    # setpriv 는 fork 하지 않고 그대로 exec 하므로 중간 프로세스가 남지 않는다.
    log "exec as ${APP_USER}(${uid}:${gid}): $*"
    exec setpriv --reuid "$uid" --regid "$gid" --init-groups -- "$@"
fi

log "running as uid=$(id -u) gid=$(id -g): $*"
exec "$@"
