#!/usr/bin/env bash
#
# claude-web 설치 스크립트 (Ubuntu Server 22.04 / 24.04)
#
#   sudo bash scripts/install.sh
#
# 이 스크립트가 하는 일은 "반복 작업"뿐입니다.
#   OS 패키지 설치 / 계정 생성 / 디렉터리 + 권한 / venv + pip / .env 뼈대
#   / systemd unit 설치
#
# 하지 않는 일 (사람이 직접 해야 합니다. DEPLOYMENT.md 참고)
#   - Claude CLI 설치와 **인증**  (브라우저가 필요합니다)
#   - 관리자 계정 생성
#   - 관리자 페이지의 Claude 설정
#   - nginx 사이트 활성화 (호스트명/인증서를 알아야 합니다)
#
# 이미 설치된 서버에서 다시 실행해도 안전합니다. .env 와 DB 는 덮어쓰지 않습니다.

set -euo pipefail

APP_USER=${APP_USER:-claudeweb}
APP_DIR=${APP_DIR:-/opt/claude-web}
DATA_DIR=${DATA_DIR:-/var/lib/claude-web}
APP_HOME="$DATA_DIR/home"
REPO=${REPO:-https://github.com/readersun/chat-bot-v1.git}
SUBDIR=${SUBDIR:-claude-web}

say()  { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m[!] %s\033[0m\n' "$*"; }

[ "$(id -u)" -eq 0 ] || { echo "root 로 실행하세요: sudo bash $0"; exit 1; }

# --- 0. OS 확인 ------------------------------------------------------------
say "OS 확인"
cat /etc/os-release | grep -E '^(NAME|VERSION)=' || true
uname -a

# --- 1. OS 패키지 ----------------------------------------------------------
say "OS 패키지 설치"
export DEBIAN_FRONTEND=noninteractive
apt-get update
# python3-venv : venv 생성 (Ubuntu 는 기본 python3 에 포함돼 있지 않다)
# sqlite3      : 백업/점검용 CLI
# ca-certificates, curl : Claude CLI 설치와 HTTPS 통신
apt-get install -y --no-install-recommends \
    git \
    python3 \
    python3-venv \
    python3-pip \
    nginx \
    sqlite3 \
    curl \
    ca-certificates \
    gnupg
# 빌드 도구(build-essential, libjpeg-dev 등)는 필요 없습니다.
# 이 앱은 Pillow 를 쓰지 않고, cryptography 는 manylinux 휠로 설치됩니다.

# --- 2. 서비스 계정 --------------------------------------------------------
say "서비스 계정 $APP_USER"
if id "$APP_USER" >/dev/null 2>&1; then
    echo "이미 있음: $APP_USER"
else
    # 홈을 /var/lib/claude-web/home 에 둔다.
    # claude CLI 인증정보가 여기(~/.claude)에 저장되고,
    # systemd unit 의 ProtectHome=true 와도 충돌하지 않는다.
    useradd --system --create-home --home-dir "$APP_HOME" \
            --shell /bin/bash --comment "Claude Web Portal" "$APP_USER"
fi

# --- 3. 소스 --------------------------------------------------------------
say "소스 배포 -> $APP_DIR"
if [ -d "$APP_DIR/.git" ]; then
    echo "이미 clone 되어 있음. git pull 은 update 절차에서 하세요."
elif [ -d "$APP_DIR" ] && [ -n "$(ls -A "$APP_DIR" 2>/dev/null)" ]; then
    warn "$APP_DIR 이 비어 있지 않습니다. 수동으로 확인하세요."
else
    tmp=$(mktemp -d)
    git clone --depth 1 "$REPO" "$tmp/src"
    mkdir -p "$APP_DIR"
    cp -a "$tmp/src/$SUBDIR/." "$APP_DIR/"
    rm -rf "$tmp"
fi
chown -R "$APP_USER:$APP_USER" "$APP_DIR"

# --- 4. 데이터 디렉터리 ----------------------------------------------------
say "데이터 디렉터리 $DATA_DIR"
# SQLite 는 DB 파일뿐 아니라 부모 디렉터리에도 쓰기 권한이 필요하다.
# WAL 모드라서 chat.db-wal / chat.db-shm 이 같은 폴더에 생긴다.
install -d -m 0750 -o "$APP_USER" -g "$APP_USER" "$DATA_DIR"
install -d -m 0750 -o "$APP_USER" -g "$APP_USER" "$DATA_DIR/uploads"
install -d -m 0750 -o "$APP_USER" -g "$APP_USER" "$DATA_DIR/workspace"
install -d -m 0750 -o "$APP_USER" -g "$APP_USER" "$DATA_DIR/backups"
install -d -m 0700 -o "$APP_USER" -g "$APP_USER" "$APP_HOME"

# --- 5. Python venv -------------------------------------------------------
say "Python 가상환경"
if [ ! -x "$APP_DIR/venv/bin/python" ]; then
    sudo -u "$APP_USER" python3 -m venv "$APP_DIR/venv"
fi
sudo -u "$APP_USER" "$APP_DIR/venv/bin/pip" install --upgrade pip wheel
sudo -u "$APP_USER" "$APP_DIR/venv/bin/pip" install -r "$APP_DIR/requirements.txt"

say "설치 확인"
sudo -u "$APP_USER" "$APP_DIR/venv/bin/python" - <<'PY'
import flask, werkzeug, dotenv, gunicorn, sqlite3
print("flask     ", flask.__version__)
print("werkzeug  ", werkzeug.__version__)
print("gunicorn  ", gunicorn.__version__)
print("sqlite3   ", sqlite3.sqlite_version)
try:
    import cryptography
    print("cryptography", cryptography.__version__, "(API Key 암호화 가능)")
except ImportError:
    print("cryptography 없음 -> API Key 는 평문으로 저장됩니다")
PY

# --- 6. .env ---------------------------------------------------------------
say ".env"
if [ -f "$APP_DIR/.env" ]; then
    echo "이미 있음. 건드리지 않습니다: $APP_DIR/.env"
else
    secret=$("$APP_DIR/venv/bin/python" -c "import secrets;print(secrets.token_hex(32))")
    cat > "$APP_DIR/.env" <<ENVEOF
# scripts/install.sh 가 생성. 필요에 따라 수정하세요.
DATABASE_PATH=$DATA_DIR/chat.db
UPLOAD_DIR=$DATA_DIR/uploads
BACKUP_DIR=$DATA_DIR/backups

HOST=127.0.0.1
PORT=8080

# 이 값이 바뀌면 전원 로그아웃되고, 저장해 둔 Claude API Key 도 복호화되지 않습니다.
SECRET_KEY=$secret

# nginx 뒤에 두므로 1. (HTTPS 로 서비스하면 SESSION_COOKIE_SECURE 도 1 로)
TRUST_PROXY=1
SESSION_COOKIE_SECURE=0
SESSION_LIFETIME_DAYS=14

MAX_UPLOAD_MB=10
MAX_IMAGES_PER_MESSAGE=5
MAX_INPUT_CHARS=8000

# 아래 CLAUDE_* 는 settings 테이블이 빈 최초 1회만 쓰입니다.
# 이후에는 관리자 페이지(/admin/claude)에서 바꿉니다.
CLAUDE_PROVIDER=cli
CLAUDE_BIN=claude
CLAUDE_WORKDIR=$DATA_DIR/workspace
CLAUDE_TIMEOUT=180
MAX_CONCURRENT_CLAUDE=3
CLAUDE_USE_RESUME=1
ENVEOF
    echo "생성함: $APP_DIR/.env (SECRET_KEY 자동 생성)"
fi
# .env 에 SECRET_KEY 가 들어 있다. 서비스 계정만 읽게 한다.
chown "$APP_USER:$APP_USER" "$APP_DIR/.env"
chmod 600 "$APP_DIR/.env"

# --- 7. DB 초기화 ----------------------------------------------------------
say "DB 스키마"
# migrate 는 기존 DB 를 지우지 않습니다. 필요한 변경만 트랜잭션으로 적용하고,
# 변경이 있으면 먼저 백업을 남깁니다. 새 서버면 빈 DB 를 만듭니다.
cd "$APP_DIR"
sudo -u "$APP_USER" HOME="$APP_HOME" "$APP_DIR/venv/bin/python" app.py migrate

# --- 8. systemd ------------------------------------------------------------
say "systemd unit"
cp "$APP_DIR/deploy/claude-web.service" /etc/systemd/system/claude-web.service
systemctl daemon-reload
systemctl enable claude-web
echo "등록 완료. 아직 start 하지 않습니다 (Claude 인증 먼저)."

# --- 9. 남은 수동 단계 안내 ------------------------------------------------
cat <<DONE

────────────────────────────────────────────────────────────────────────
자동 설치가 끝났습니다. 아래는 직접 해야 합니다. (DEPLOYMENT.md 참고)

 1) Claude CLI 설치
      sudo install -d -m 0755 /etc/apt/keyrings
      sudo curl -fsSL https://downloads.claude.ai/keys/claude-code.asc \
           -o /etc/apt/keyrings/claude-code.asc
      gpg --show-keys /etc/apt/keyrings/claude-code.asc
      # 지문이 31DDDE24DDFAB679F42D7BD2BAA929FF1A7ECACE 인지 확인
      echo "deb [signed-by=/etc/apt/keyrings/claude-code.asc] \
https://downloads.claude.ai/claude-code/apt/stable stable main" \
        | sudo tee /etc/apt/sources.list.d/claude-code.list
      sudo apt update && sudo apt install claude-code

 2) **서비스 계정으로** Claude 인증 (여기가 가장 자주 빠뜨리는 단계입니다)
      sudo -u $APP_USER -H claude auth login
      sudo -u $APP_USER -H claude -p "Respond only with OK"

 3) 서비스 시작
      sudo systemctl start claude-web
      systemctl status claude-web

 4) 관리자 계정
      sudo -u $APP_USER -H $APP_DIR/venv/bin/python $APP_DIR/app.py create-admin

 5) nginx
      sudo cp $APP_DIR/deploy/nginx-http.conf.example \
              /etc/nginx/sites-available/claude-web
      # server_name 을 사내 호스트명으로 수정한 뒤
      sudo ln -sf /etc/nginx/sites-available/claude-web /etc/nginx/sites-enabled/
      sudo rm -f /etc/nginx/sites-enabled/default
      sudo nginx -t && sudo systemctl reload nginx
────────────────────────────────────────────────────────────────────────
DONE
