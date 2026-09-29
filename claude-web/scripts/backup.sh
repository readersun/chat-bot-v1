#!/usr/bin/env bash
#
# claude-web 백업 (DB + 업로드 이미지)
#
#   sudo bash scripts/backup.sh                 -> /var/backups/claude-web 에 저장
#   sudo BACKUP_ROOT=/mnt/nas bash scripts/backup.sh
#
# 업데이트 배포 전에 반드시 한 번 돌리세요.
#
# DB 는 cp 가 아니라 sqlite3 의 online backup(.backup)을 씁니다.
# WAL 모드라 서비스가 돌고 있는 중에 cp 하면 -wal 파일과 어긋난 스냅샷이
# 나올 수 있습니다. .backup 은 실행 중에도 일관된 사본을 만듭니다.

set -euo pipefail

DATA_DIR=${DATA_DIR:-/var/lib/claude-web}
APP_DIR=${APP_DIR:-/opt/claude-web}
BACKUP_ROOT=${BACKUP_ROOT:-/var/backups/claude-web}
KEEP=${KEEP:-14}                       # 보관 일수
STAMP=$(date +%Y%m%d-%H%M%S)

command -v sqlite3 >/dev/null || { echo "sqlite3 가 없습니다: sudo apt install -y sqlite3"; exit 1; }

install -d -m 0700 "$BACKUP_ROOT"

# --- DB -------------------------------------------------------------------
db="$DATA_DIR/chat.db"
if [ -f "$db" ]; then
    out="$BACKUP_ROOT/chat-$STAMP.db"
    sqlite3 "$db" ".backup '$out'"
    # 무결성까지 확인해 둔다. 깨진 백업은 백업이 아니다.
    ok=$(sqlite3 "$out" "PRAGMA integrity_check;")
    [ "$ok" = "ok" ] || { echo "백업 무결성 실패: $ok"; exit 1; }
    chmod 600 "$out"
    echo "DB      : $out  ($(du -h "$out" | cut -f1), integrity_check=ok)"
else
    echo "DB 없음 : $db"
fi

# --- 업로드 이미지 ---------------------------------------------------------
if [ -d "$DATA_DIR/uploads" ]; then
    out="$BACKUP_ROOT/uploads-$STAMP.tar.gz"
    tar -czf "$out" -C "$DATA_DIR" uploads
    chmod 600 "$out"
    echo "uploads : $out  ($(du -h "$out" | cut -f1))"
fi

# --- .env -----------------------------------------------------------------
# SECRET_KEY 가 들어 있습니다. 이게 바뀌면 전원 로그아웃되고, DB 에 저장해 둔
# Claude API Key 도 복호화되지 않습니다. DB 와 같은 등급으로 보관하세요.
if [ -f "$APP_DIR/.env" ]; then
    out="$BACKUP_ROOT/env-$STAMP"
    install -m 600 "$APP_DIR/.env" "$out"
    echo ".env    : $out  (SECRET_KEY 포함. 취급 주의)"
fi

# --- 오래된 백업 정리 ------------------------------------------------------
find "$BACKUP_ROOT" -maxdepth 1 -type f -mtime "+$KEEP" -print -delete \
    | sed 's/^/삭제    : /' || true

echo
echo "보관 위치: $BACKUP_ROOT (최근 ${KEEP}일)"
df -h "$BACKUP_ROOT" | tail -1
echo
echo "주의: Claude CLI 인증정보($DATA_DIR/home/.claude/.credentials.json)는"
echo "      여기에 넣지 않았습니다. 장기 유효 자격증명이라 일반 백업과 같은 곳에"
echo "      두면 안 됩니다. 서버를 다시 만들 때는 백업 복원 대신"
echo "      'sudo -u claudeweb -H claude auth login' 으로 다시 인증하세요."
