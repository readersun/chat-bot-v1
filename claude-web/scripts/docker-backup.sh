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
docker compose exec -T app sh -c '
    set -e
    out="$1"; stamp="$2"
    cp /app/.env "$out/env-$stamp.bak"
    chmod 600 "$out/env-$stamp.bak"
    echo "설정 백업  : $out/env-$stamp.bak (0600)"
' sh "$OUT" "$STAMP"

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
