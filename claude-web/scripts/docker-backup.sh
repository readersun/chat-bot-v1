#!/usr/bin/env bash
#
# Docker 배포용 백업
#
#   ./scripts/docker-backup.sh [보관일수]        (기본 14일)
#
# 모든 작업을 **컨테이너 안에서** 수행한다. 그래서
#   - host 에 sqlite3 / tar 를 설치할 필요가 없다
#   - host 에서 /var/lib/claude-web 에 쓸 권한(root)이 필요 없다
#   - HOST_DATA_DIR 을 어디로 바꿔도 스크립트를 고칠 필요가 없다
#
# 백업 대상
#   1) chat.db   -> `python app.py backup` = sqlite 온라인 백업 API.
#                   WAL 을 쓰므로 단순 cp 로는 일관성이 보장되지 않는다.
#   2) uploads/  -> tar.gz   (채팅 첨부 이미지)
#   3) notes/    -> tar.gz   (메모 첨부파일)
#   4) .env      -> SECRET_KEY 가 들어 있다. 잃으면 전원 로그아웃 + 관리자 화면에
#                   저장한 Claude API Key 를 복호화할 수 없다.
#   5) Claude 인증 -> **일부러 제외한다.** 맨 아래 설명 참고.
#
# 결과물은 컨테이너의 BACKUP_DIR(기본 /var/lib/claude-web/backups) 에 쌓인다.
# 이 경로는 host 의 HOST_DATA_DIR 아래이므로 host 의 일반 백업 도구로 그대로
# 가져가면 된다.
#
set -euo pipefail

cd "$(dirname "$0")/.."            # = compose 프로젝트 디렉터리 (claude-web/)

KEEP="${1:-14}"
STAMP="$(date +%Y%m%d-%H%M%S)"

echo "== claude-web 백업 =="

if ! docker compose ps --status running --services 2>/dev/null | grep -qx app; then
    echo "!! app 컨테이너가 실행 중이 아닙니다." >&2
    echo "   백업은 컨테이너 안에서 수행하므로 먼저 띄워야 합니다: docker compose up -d" >&2
    exit 1
fi

# 앱이 실제로 쓰는 경로를 앱에게 물어본다. (.env 를 다시 해석하지 않는다)
DATA_DIR="$(docker compose exec -T app python -c \
    "import config,os;print(os.path.dirname(config.DATABASE_PATH))" | tr -d '\r')"
UP_DIR="$(docker compose exec -T app python -c \
    "import config;print(config.UPLOAD_DIR)" | tr -d '\r')"
NOTES_DIR="$(docker compose exec -T app python -c \
    "import config;print(config.NOTES_DIR)" | tr -d '\r')"
OUT="$(docker compose exec -T app python -c \
    "import config;print(config.BACKUP_DIR)" | tr -d '\r')"

echo "   DB      : $DATA_DIR/chat.db"
echo "   uploads : $UP_DIR"
echo "   notes   : $NOTES_DIR"
echo "   저장    : $OUT  (컨테이너 경로 = host 의 HOST_DATA_DIR 아래)"
echo

# --- 1) DB : sqlite 온라인 백업 + 무결성 검증 -------------------------------
docker compose exec -T app python app.py backup
docker compose exec -T app python - "$OUT" <<'PYCHK'
import glob, os, sqlite3, sys
out = sys.argv[1]
cands = sorted(glob.glob(os.path.join(out, "chat.db.backup-*")), key=os.path.getmtime)
if not cands:
    print("!! 백업 파일을 찾지 못했습니다."); sys.exit(1)
newest = cands[-1]
conn = sqlite3.connect(newest)
try:
    res = conn.execute("PRAGMA integrity_check").fetchone()[0]
    n = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
finally:
    conn.close()
print("무결성 검증 : %s -> %s (users %d행)" % (os.path.basename(newest), res, n))
sys.exit(0 if res == "ok" else 1)
PYCHK

# --- 2) 업로드 --------------------------------------------------------------
docker compose exec -T app sh -c '
    set -e
    out="$1"; up="$2"; stamp="$3"
    mkdir -p "$out"
    tar -czf "$out/uploads-$stamp.tar.gz" -C "$(dirname "$up")" "$(basename "$up")"
    echo "업로드 백업: $out/uploads-$stamp.tar.gz"
' sh "$OUT" "$UP_DIR" "$STAMP"

# --- 2-2) 메모 첨부 --------------------------------------------------------
# 디렉터리가 아직 없을 수도 있다. (메모 기능을 아직 쓰지 않은 서버)
# 그때는 건너뛰고 백업 전체를 실패시키지 않는다.
docker compose exec -T app sh -c '
    set -e
    out="$1"; nt="$2"; stamp="$3"
    if [ -d "$nt" ]; then
        tar -czf "$out/notes-$stamp.tar.gz" -C "$(dirname "$nt")" "$(basename "$nt")"
        echo "메모 백업  : $out/notes-$stamp.tar.gz"
    else
        echo "메모 백업  : 건너뜀 ($nt 없음)"
    fi
' sh "$OUT" "$NOTES_DIR" "$STAMP"

# --- 3) .env (SECRET_KEY 포함 -> 0600) --------------------------------------
#
# 컨테이너는 uid 1000 으로 돌기 때문에 .env 가 root:root 0600 이면 읽지 못한다.
# 예전에는 그때 cp 가 실패하면서 set -e 에 걸려 **백업 전체가 실패**했고,
# 이미 끝난 DB/첨부 백업까지 함께 버려진 뒤 docker-update.sh 가 배포를 멈췄다.
#
# .env 는 이 스크립트가 만들거나 고치는 파일이 아니다. 복사에 실패해도 원본은
# 그대로 있고, 업데이트(git pull + restart)가 .env 를 건드리지도 않는다.
# 그러므로 여기서 멈출 이유가 없다. 경고만 남기고 넘어간다.
#
# 대신 두 번 시도한다.
#   1) 컨테이너 안에서 (평소 경로)
#   2) host 에서 직접 (이 스크립트는 보통 root 로 돈다)
ENV_OK=0

if docker compose exec -T app sh -c '
        set -e
        out="$1"; stamp="$2"
        cp /app/.env "$out/env-$stamp.bak"
        chmod 600 "$out/env-$stamp.bak"
        echo "설정 백업  : $out/env-$stamp.bak (0600)"
    ' sh "$OUT" "$STAMP" 2>/dev/null; then
    ENV_OK=1
else
    # host 쪽 경로를 계산한다. 컨테이너의 /var/lib/claude-web 이 host 의
    # HOST_DATA_DIR 에 bind mount 되어 있다. (compose.yml 의 volumes)
    HOST_DATA_DIR="$(grep -E '^HOST_DATA_DIR=' .env 2>/dev/null | tail -1 | cut -d= -f2- || true)"
    HOST_DATA_DIR="${HOST_DATA_DIR:-/var/lib/claude-web}"
    HOST_OUT="${OUT/#\/var\/lib\/claude-web/$HOST_DATA_DIR}"

    if [ -r .env ] && [ -d "$HOST_OUT" ]; then
        if cp .env "$HOST_OUT/env-$STAMP.bak" 2>/dev/null; then
            chmod 600 "$HOST_OUT/env-$STAMP.bak"
            echo "설정 백업  : $HOST_OUT/env-$STAMP.bak (0600, host 에서 복사)"
            ENV_OK=1
        fi
    fi
fi

if [ "$ENV_OK" = "0" ]; then
    echo
    echo "!! .env 는 이번 백업에 포함되지 않았습니다. (DB 와 첨부파일은 정상입니다)"
    echo "   컨테이너가 uid 1000 으로 돌아서 root:root 0600 인 .env 를 못 읽습니다."
    echo "   다음을 실행하면 다음 백업부터 함께 담깁니다:"
    echo "       chown root:1000 .env && chmod 640 .env"
    echo "   .env 에는 SECRET_KEY 가 들어 있습니다. 잃으면 전원 로그아웃되고"
    echo "   관리자 화면에 저장한 Claude API Key 를 복호화할 수 없습니다."
    echo
fi

# --- 4) 오래된 백업 정리 ----------------------------------------------------
docker compose exec -T app sh -c '
    out="$1"; keep="$2"
    find "$out" -maxdepth 1 -type f \
        \( -name "chat.db.backup-*" -o -name "uploads-*.tar.gz" -o -name "notes-*.tar.gz" -o -name "env-*.bak" \) \
        -mtime "+$keep" -print -delete 2>/dev/null || true
' sh "$OUT" "$KEEP"

echo
docker compose exec -T app sh -c 'ls -lh "$1" | tail -12' sh "$OUT"

cat <<'NOTE'

Claude 인증(claude-web-home 볼륨)은 이 백업에 포함되지 않습니다.
  - 그 볼륨에는 사용자 계정으로 로그인한 OAuth 자격증명이 들어 있습니다.
    (~/.claude/.credentials.json, 0600)
  - 평문 자격증명을 백업 파일로 복사해 두면 보관/유출 위험이 그만큼 커집니다.
  - 잃어버려도 복구는 간단합니다. 다시 로그인하면 됩니다.
        docker compose exec app claude
  - 그래도 보관해야 한다면 root 로, 접근이 제한된 곳에만 두세요.
        docker run --rm -v claude-web-home:/h -v "$PWD":/out alpine \
            tar -czf /out/claude-home.tar.gz -C /h .
        chmod 600 claude-home.tar.gz
NOTE
