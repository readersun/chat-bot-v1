# claude-web 운영 배포 가이드

아무것도 설치되어 있지 않은 **Ubuntu Server 22.04 / 24.04** 한 대에 처음부터
설치하는 절차입니다. 위에서부터 순서대로 따라 하면 됩니다.

검증 환경: Ubuntu Server 22.04 LTS / 24.04 LTS (x86_64).
배포판 종속 코드는 없습니다. Debian 12, RHEL 9 에서도 패키지 관리자 명령만
바꾸면 동작합니다. (`apt` → `dnf`, 서비스 계정 생성 옵션 동일)

---

## 0. 최종 구성

```
                브라우저 (사내망)
                      │
                      ▼
          nginx  :80 / :443          ← 여기만 방화벽에 연다
                      │  proxy_pass
                      ▼
       gunicorn  127.0.0.1:8080      ← 워커 1개 + 스레드 8개
                      │
                 Flask (app:app)
                      │  subprocess
                      ▼
              claude -p  (Claude Code CLI)
```

| 항목 | 경로 |
| --- | --- |
| 소스 | `/opt/claude-web` |
| 가상환경 | `/opt/claude-web/venv` |
| 서버 설정 | `/opt/claude-web/.env` (0600) |
| DB | `/var/lib/claude-web/chat.db` |
| 채팅 첨부 이미지 | `/var/lib/claude-web/uploads/` |
| 메모 첨부파일 | `/var/lib/claude-web/notes/` |
| Claude 작업 디렉터리 | `/var/lib/claude-web/workspace/` |
| 서비스 계정 HOME | `/var/lib/claude-web/home` (Claude 인증정보가 여기 있음) |
| 자동 백업 | `/var/lib/claude-web/backups/` |
| 로그 | systemd journal (별도 로그 파일 없음) |

> **빠른 길**: `sudo bash scripts/install.sh` 가 2·3·4·5·6·7·8·12 단계를 대신 합니다.
> 다만 Claude CLI 설치/인증(9·10), 관리자 생성(13), nginx(15), 관리자 설정(14)은
> 사람이 직접 해야 합니다. 처음 배포한다면 스크립트를 쓰더라도 이 문서를 한 번은
> 읽고 진행하세요.

---

## 1. OS 확인

```bash
cat /etc/os-release
uname -a
df -h /
free -m
```

요구사항: Ubuntu 20.04 이상, 메모리 4GB 이상(Claude CLI 권장), 여유 디스크 10GB 이상.
Python 은 3.8 이상이면 되고, 22.04 의 3.10 / 24.04 의 3.12 모두 그대로 씁니다.

---

## 2. OS 패키지

```bash
sudo apt update
sudo apt install -y --no-install-recommends \
    git \
    python3 \
    python3-venv \
    python3-pip \
    nginx \
    sqlite3 \
    curl \
    ca-certificates \
    gnupg
```

각 패키지가 왜 필요한지:

| 패키지 | 용도 |
| --- | --- |
| `git` | 소스 배포와 이후 업데이트 |
| `python3`, `python3-venv`, `python3-pip` | 앱 실행. Ubuntu 는 `venv` 가 기본 포함이 아니라 따로 필요 |
| `nginx` | 리버스 프록시 |
| `sqlite3` | 백업(`.backup`)과 점검(`PRAGMA integrity_check`) |
| `curl`, `ca-certificates`, `gnupg` | Claude CLI 설치와 저장소 키 검증 |

**필요 없는 것**: `build-essential`, `libjpeg-dev`, `zlib1g-dev` 는 설치하지 않습니다.
이 앱은 Pillow 를 쓰지 않고(이미지 검증은 매직바이트 직접 확인),
`cryptography` 는 manylinux 휠로 설치되어 컴파일이 없습니다.
Redis / PostgreSQL / Docker / Node.js 도 필요 없습니다.

---

## 3. 서비스 계정

```bash
sudo useradd --system --create-home \
             --home-dir /var/lib/claude-web/home \
             --shell /bin/bash \
             --comment "Claude Web Portal" claudeweb
```

**HOME 을 `/var/lib/claude-web/home` 으로 두는 이유**: Claude CLI 는 인증정보를
`$HOME/.claude/.credentials.json` 에 저장합니다. 홈을 데이터 디렉터리 아래 두면
systemd unit 에서 `ProtectHome=true` 로 `/home` 전체를 막아도 Claude 가 정상
동작하고, 백업 대상 경로도 한 군데로 모입니다.

`--shell /bin/bash` 는 필요합니다. `sudo -u claudeweb -H claude auth login` 으로
인증할 때 셸이 있어야 합니다. (`/usr/sbin/nologin` 이면 인증 단계에서 막힙니다)

---

## 4. 소스 배포

```bash
sudo git clone https://github.com/readersun/chat-bot-v1.git /tmp/claude-web-src
sudo mkdir -p /opt/claude-web
sudo cp -a /tmp/claude-web-src/claude-web/. /opt/claude-web/
sudo rm -rf /tmp/claude-web-src
sudo chown -R claudeweb:claudeweb /opt/claude-web
```

저장소 루트가 아니라 **`claude-web/` 하위 디렉터리**가 애플리케이션입니다.

`/opt/claude-web` 에서 바로 `git pull` 하고 싶다면 대신 이렇게 합니다.

```bash
sudo git clone https://github.com/readersun/chat-bot-v1.git /opt/claude-web-repo
sudo ln -s /opt/claude-web-repo/claude-web /opt/claude-web
sudo chown -R claudeweb:claudeweb /opt/claude-web-repo
```

### 저장소에 들어가면 안 되는 것 (확인 완료)

`.gitignore` 가 다음을 전부 제외합니다. 새 서버에서 `git status` 가 깨끗한지
한 번 확인하세요.

```
.env                  SECRET_KEY 가 들어 있다
data/                 chat.db, uploads/, notes/, backups/, setup-token.txt
venv/ __pycache__/ *.pyc
.credentials.json     Claude 인증정보
claude-auth.env       CLAUDE_CODE_OAUTH_TOKEN
*.pem *.key *.crt     TLS 인증서
```

---

## 5. 데이터 디렉터리

```bash
sudo install -d -m 0750 -o claudeweb -g claudeweb /var/lib/claude-web
sudo install -d -m 0750 -o claudeweb -g claudeweb /var/lib/claude-web/uploads
sudo install -d -m 0750 -o claudeweb -g claudeweb /var/lib/claude-web/notes
sudo install -d -m 0750 -o claudeweb -g claudeweb /var/lib/claude-web/workspace
sudo install -d -m 0750 -o claudeweb -g claudeweb /var/lib/claude-web/backups
sudo install -d -m 0700 -o claudeweb -g claudeweb /var/lib/claude-web/home
```

**SQLite 는 DB 파일만이 아니라 부모 디렉터리에도 쓰기 권한이 필요합니다.**
WAL 모드로 동작하므로 `chat.db-wal` / `chat.db-shm` 이 같은 폴더에 생깁니다.
`/var/lib/claude-web` 자체가 `claudeweb` 소유여야 합니다.

확인:

```bash
sudo -u claudeweb touch /var/lib/claude-web/.wtest && \
sudo -u claudeweb rm /var/lib/claude-web/.wtest && echo "DB 디렉터리 쓰기 OK"

sudo -u claudeweb touch /var/lib/claude-web/uploads/.wtest && \
sudo -u claudeweb rm /var/lib/claude-web/uploads/.wtest && echo "uploads 쓰기 OK"
```

---

## 6. Python 가상환경

```bash
cd /opt/claude-web
sudo -u claudeweb python3 -m venv venv
sudo -u claudeweb venv/bin/pip install --upgrade pip wheel
sudo -u claudeweb venv/bin/pip install -r requirements.txt
```

설치되는 것은 5개(+의존성)뿐입니다.

| 패키지 | 왜 필요한가 |
| --- | --- |
| `Flask` | 웹 프레임워크 |
| `Werkzeug` | 비밀번호 해시(`generate_password_hash`), 파일 응답. Flask 가 끌고 오지만 보안 업데이트를 직접 받으려고 명시 |
| `python-dotenv` | `.env` 읽기 |
| `gunicorn` | 운영 WSGI 서버 |
| `cryptography` | 관리자 페이지에 저장하는 Claude API Key 암호화 |

개발 PC 에 있는 `pillow`, `requests` 는 **앱이 import 하지 않습니다.**
테스트 도구용이라 서버에 설치하지 않습니다.

확인:

```bash
sudo -u claudeweb venv/bin/python - <<'PY'
import flask, werkzeug, dotenv, gunicorn, sqlite3
print("flask       ", flask.__version__)
print("werkzeug    ", werkzeug.__version__)
print("gunicorn    ", gunicorn.__version__)
print("sqlite3     ", sqlite3.sqlite_version)
try:
    import cryptography
    print("cryptography", cryptography.__version__, "-> API Key 암호화 가능")
except ImportError:
    print("cryptography 없음 -> API Key 가 평문 저장됩니다")
PY
```

---

## 7. `.env` 작성

### SECRET_KEY 생성

```bash
/opt/claude-web/venv/bin/python -c "import secrets; print(secrets.token_hex(32))"
```

> **이 값은 한 번 정하면 바꾸지 마세요.** 바꾸면 (1) 전원 로그아웃되고
> (2) 관리자 페이지에 저장한 Claude API Key 가 이 값에서 파생한 키로
> 암호화돼 있어 복호화되지 않습니다. 백업 대상입니다.

### 파일 작성

```bash
sudo -u claudeweb tee /opt/claude-web/.env >/dev/null <<'EOF'
# --- 저장 위치 ---
DATABASE_PATH=/var/lib/claude-web/chat.db
UPLOAD_DIR=/var/lib/claude-web/uploads
NOTES_DIR=/var/lib/claude-web/notes
BACKUP_DIR=/var/lib/claude-web/backups

# --- 웹 서버 (nginx 뒤. 루프백만 듣는다) ---
HOST=127.0.0.1
PORT=8080

# --- 위에서 생성한 값으로 교체 ---
SECRET_KEY=여기에_붙여넣기

# --- 프록시 / 쿠키 ---
TRUST_PROXY=1
SESSION_COOKIE_SECURE=0        # HTTPS 로 서비스하면 1
SESSION_LIFETIME_DAYS=14

# --- 업로드 / 입력 제한 (nginx client_max_body_size 와 맞출 것) ---
MAX_UPLOAD_MB=10
MAX_IMAGES_PER_MESSAGE=5
MAX_INPUT_CHARS=8000

# --- Claude 초기값 (settings 테이블이 빈 최초 1회만 사용) ---
CLAUDE_PROVIDER=cli
CLAUDE_BIN=claude
CLAUDE_WORKDIR=/var/lib/claude-web/workspace
CLAUDE_TIMEOUT=180
MAX_CONCURRENT_CLAUDE=3
CLAUDE_USE_RESUME=1
EOF

sudo chown claudeweb:claudeweb /opt/claude-web/.env
sudo chmod 600 /opt/claude-web/.env
```

**설정이 두 군데로 나뉘는 구조**입니다. 이 경계를 지키세요.

| | 어디에 | 언제 바뀌나 |
| --- | --- | --- |
| 서버 고정 설정 (경로/포트/키/쿠키/로그인 정책) | `.env` | 배포 시. 바꾸면 재시작 필요 |
| Claude 연결 설정 (provider/CLI 경로/timeout/동시 실행) | **DB settings 테이블** | 운영 중 관리자 페이지에서. 재시작 불필요 |

`.env` 의 `CLAUDE_*` 는 settings 테이블이 **비어 있는 최초 1회만** 초기값으로
쓰입니다. 그 뒤로 여기를 고쳐도 반영되지 않습니다. `/admin/claude` 에서 바꾸세요.

`.env` 는 systemd 의 `EnvironmentFile` 로도 읽힙니다. systemd 형식이므로
`export` 를 붙이거나 값 뒤에 `# 주석` 을 달지 마세요. (`KEY=value` 한 줄씩)

---

## 8. DB 초기화

```bash
cd /opt/claude-web
sudo -u claudeweb HOME=/var/lib/claude-web/home venv/bin/python app.py migrate
```

- **새 서버**: 빈 DB 를 만들고 스키마를 `user_version = 3` 으로 맞춥니다.
- **기존 DB 를 옮겨 온 경우**: 필요한 변경만 적용합니다. 기존 DB 를 지우지
  않으며, 변경이 있으면 **먼저 백업**을 `backups/` 에 남기고 모든 DDL/DML 을
  하나의 트랜잭션에서 실행합니다. 중간에 실패하면 전부 롤백되어 원본이 그대로
  남습니다. 이미 최신이면 "변경할 내용이 없습니다" 를 출력하고 끝납니다.
  매번 실행해도 안전합니다.

확인:

```bash
sudo -u claudeweb sqlite3 /var/lib/claude-web/chat.db \
  "PRAGMA user_version; PRAGMA journal_mode; PRAGMA integrity_check;"
# 3 / wal / ok
```

---

## 9. Claude CLI 설치

**apt 저장소 방식을 권장합니다.** Node.js 가 필요 없고, 바이너리가
`/usr/bin/claude` 에 놓여 서비스 계정이 그대로 쓸 수 있으며, 백그라운드
자동 업데이트가 없어서 운영 중에 버전이 바뀌지 않습니다.

```bash
sudo install -d -m 0755 /etc/apt/keyrings
sudo curl -fsSL https://downloads.claude.ai/keys/claude-code.asc \
     -o /etc/apt/keyrings/claude-code.asc

# 키가 Anthropic 것인지 반드시 확인
gpg --show-keys /etc/apt/keyrings/claude-code.asc
# 지문이 31DDDE24DDFAB679F42D7BD2BAA929FF1A7ECACE 여야 합니다

echo "deb [signed-by=/etc/apt/keyrings/claude-code.asc] https://downloads.claude.ai/claude-code/apt/stable stable main" \
  | sudo tee /etc/apt/sources.list.d/claude-code.list
sudo apt update
sudo apt install -y claude-code
```

확인:

```bash
which claude          # /usr/bin/claude
claude --version      # 예: 2.1.263 (Claude Code)
claude --help | head  # 사용법 확인
claude doctor         # 설치 상태 진단
```

<details>
<summary>대안 1 — 네이티브 설치 스크립트</summary>

apt 저장소에 접근할 수 없을 때 씁니다. **반드시 서비스 계정으로** 실행하세요.
설치 위치가 실행한 사용자의 `~/.local/bin` 이라, root 로 실행하면
`claudeweb` 계정에서 쓸 수 없습니다.

```bash
sudo -u claudeweb -H bash -lc 'curl -fsSL https://claude.ai/install.sh | bash'
sudo -u claudeweb -H bash -lc 'claude --version'
# -> /var/lib/claude-web/home/.local/bin/claude
```

이 방식은 백그라운드 자동 업데이트가 켜집니다. 운영 중 버전이 바뀌는 것이
싫으면 `/var/lib/claude-web/home/.claude/settings.json` 에 넣으세요.

```json
{ "env": { "DISABLE_AUTOUPDATER": "1" } }
```
</details>

<details>
<summary>대안 2 — npm</summary>

Node.js 22 이상이 필요합니다. 이 앱 자체는 Node 를 쓰지 않으므로
Claude CLI 때문에만 Node 를 까는 셈이라 권장하지 않습니다.

```bash
node --version    # v22 이상
npm --version
npm install -g @anthropic-ai/claude-code     # sudo 를 붙이지 말 것
```
</details>

---

## 10. Claude 인증 — 가장 자주 빠뜨리는 단계

> 본인 SSH 계정에서 `claude -p` 가 된다고 끝난 것이 아닙니다.
> Claude 인증은 **계정 단위(`$HOME/.claude/.credentials.json`)** 입니다.
> 웹 서비스는 `claudeweb` 계정으로 돌기 때문에, **그 계정에서** 되어야 합니다.

### 방법 A — 서비스 계정으로 직접 로그인 (권장)

```bash
sudo -u claudeweb -H claude auth login
```

서버에 브라우저가 없으면 터미널에 로그인 URL 이 뜹니다. 그 URL 을 자기 PC
브라우저에서 열어 로그인한 뒤, 화면에 나오는 코드를 터미널에
`Paste code here if prompted` 프롬프트에 붙여 넣으면 됩니다.
(SSH 세션에서 흔한 경로입니다)

인증정보는 `/var/lib/claude-web/home/.claude/.credentials.json` 에
`0600` 으로 저장됩니다.

### 방법 B — 장기 토큰 (완전 headless / CI)

브라우저 흐름을 서버에서 전혀 쓸 수 없을 때. 1년짜리 OAuth 토큰을 만듭니다.
(Pro / Max / Team / Enterprise 구독 필요)

```bash
claude setup-token        # 아무 계정에서나 실행. 토큰이 화면에 출력됨
```

출력된 토큰을 **root 만 읽는 별도 파일**에 넣습니다.
`.env` 에 넣지 마세요. `.env` 는 서비스 계정이 읽을 수 있고 백업에도 섞입니다.

```bash
sudo install -d -m 0700 /etc/claude-web
sudo tee /etc/claude-web/claude-auth.env >/dev/null <<'EOF'
CLAUDE_CODE_OAUTH_TOKEN=여기에_토큰
EOF
sudo chmod 600 /etc/claude-web/claude-auth.env
sudo chown root:root /etc/claude-web/claude-auth.env
```

systemd unit 이 이 파일을 `EnvironmentFile=-` 로 읽습니다. (없으면 그냥 넘어감)
systemd 는 root 로 파일을 읽은 뒤 권한을 낮춰 프로세스를 띄우므로,
파일 자체는 `claudeweb` 이 못 읽어도 환경변수는 전달됩니다.

### 방법 C — Anthropic Console API Key

구독이 아니라 API 사용량 과금을 쓸 때. 이때는 CLI 대신 앱의
**API provider** 를 쓰는 편이 낫습니다. (14단계에서 provider 를 `api` 로
고르고 관리자 페이지에 API Key 를 입력 → DB 에 암호화 저장)

### 인증 확인 (반드시)

```bash
sudo -u claudeweb -H claude auth status --text
sudo -u claudeweb -H claude -p "Respond only with OK"
```

`OK` 가 나오면 성공입니다. 안 되면 실제 환경으로 들어가서 확인하세요.

```bash
sudo -u claudeweb -H bash
  echo $HOME              # /var/lib/claude-web/home
  which claude
  claude --version
  ls -l ~/.claude/.credentials.json
  claude -p "Respond only with OK"
  exit
```

---

## 11. 외부 통신 / 프록시 확인

Claude CLI 는 Anthropic 에 HTTPS 로 나갑니다. 먼저 있는 그대로 확인하세요.

```bash
sudo -u claudeweb -H claude -p "Respond only with OK"
```

이게 되면 추가 설정이 필요 없습니다. **되는데 프록시를 넣지 마세요.**

사내에서 아웃바운드가 프록시를 통해야만 나가는 환경이면, 사내 정책에 맞는
값을 systemd unit 에 추가합니다. (임의로 만들지 말고 네트워크 담당에게 확인)

```ini
# /etc/systemd/system/claude-web.service.d/proxy.conf
[Service]
Environment=HTTPS_PROXY=http://proxy.example.local:3128
Environment=HTTP_PROXY=http://proxy.example.local:3128
Environment=NO_PROXY=localhost,127.0.0.1,.example.local
```

```bash
sudo systemctl daemon-reload && sudo systemctl restart claude-web
```

셸에서 먼저 시험해 볼 때:

```bash
sudo -u claudeweb -H env HTTPS_PROXY=http://proxy.example.local:3128 \
     claude -p "Respond only with OK"
```

---

## 12. systemd 서비스

```bash
sudo cp /opt/claude-web/deploy/claude-web.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable claude-web
sudo systemctl start claude-web
systemctl status claude-web
```

로그:

```bash
journalctl -u claude-web -f          # 실시간
journalctl -u claude-web -n 100      # 최근 100줄
journalctl -u claude-web --since "10 min ago"
```

`deploy/claude-web.service` 에서 꼭 알아야 할 항목:

| 항목 | 이유 |
| --- | --- |
| `User=claudeweb` | Claude 인증이 계정 단위. root 로 띄우면 인증정보를 못 찾습니다 |
| `Environment=HOME=/var/lib/claude-web/home` | **없으면 `claude -p` 가 인증 실패합니다.** 가장 흔한 사고 |
| `Environment=PATH=...` | systemd 는 로그인 셸의 PATH 를 물려받지 않습니다. `claude` 위치를 명시 |
| `EnvironmentFile=-/etc/claude-web/claude-auth.env` | 10-B 방식일 때만 쓰임. `-` 는 "없어도 기동" |
| `StateDirectory=claude-web` | `/var/lib/claude-web` 을 systemd 가 만들고 소유권을 맞춤 |
| `Type=notify` | gunicorn 이 `READY=1` 을 보냅니다. 기동 완료를 systemd 가 정확히 압니다 |
| `ProtectSystem=strict` + `ReadWritePaths=/var/lib/claude-web` | 데이터 디렉터리 외에는 전부 읽기 전용 |
| `ProtectHome=true` | `/home` 차단. HOME 이 `/var/lib` 아래라 문제없음 |

> 보안 강화 항목 때문에 Claude 가 실패하는 것 같으면
> `ProtectSystem` → `ProtectHome` → `NoNewPrivileges` 순으로 하나씩 주석 처리하며
> 범위를 좁히세요. 원인을 찾으면 그 줄만 완화하고 나머지는 유지합니다.

### 왜 gunicorn 워커가 1개인가 (중요)

앱은 다음 세 가지를 **프로세스 메모리**에 둡니다.

```
app.ConcurrencyLimiter   전체 동시 Claude 실행 수 제한 (max_concurrent_claude)
app._SESSION_LOCKS       같은 세션 동시 요청 차단 (public 세션 문맥 보호)
auth._setup_token        최초 관리자 bootstrap 토큰
```

gunicorn 워커는 별도 프로세스라 이 상태가 공유되지 않습니다.
워커를 N개로 늘리면 조용히 다음이 깨집니다.

- 동시 실행 제한이 사실상 N배가 된다 (3으로 설정해도 실제 3×N)
- 같은 public 세션에 두 사람이 동시에 보내면 서로 다른 워커에 걸려 세션 lock 을
  통과하고, `claude --resume` 문맥이 섞인다
- `/setup` 의 bootstrap 토큰이 워커마다 달라 최초 관리자 생성이 간헐 실패한다

그래서 **워커 1개 + 스레드 8개**로 동시성을 냅니다. 개발 서버
(`app.run(threaded=True)`)와 동작이 같습니다. 로그인 rate limit 과 설정값은
SQLite 에 있어 영향받지 않습니다. 사내 수십 명 규모에는 이 구성으로 충분합니다.

더 키워야 하면 워커를 늘리기 전에 위 세 가지를 DB/파일 lock 으로 옮겨야 합니다.

### 타임아웃 3단 (충돌하면 안 됨)

```
Claude timeout   180초   /admin/claude (DB settings)
gunicorn timeout 300초   deploy/gunicorn.conf.py  (GUNICORN_TIMEOUT)
nginx read       360초   proxy_read_timeout
```

**반드시 Claude < gunicorn < nginx** 순서여야 합니다. 어긋나면 Claude 가
답하는 중인데 앞단이 먼저 끊어 502 / 504 가 납니다.
관리자 페이지에서 Claude timeout 을 올리면 나머지 둘도 함께 올리세요.

---

## 13. 최초 관리자 계정

두 가지 방법이 있습니다. 구현된 그대로입니다.

### 방법 A — 셸에서 바로 (권장)

```bash
sudo -u claudeweb -H /opt/claude-web/venv/bin/python /opt/claude-web/app.py create-admin
```

아이디 / 표시 이름 / 비밀번호(8자 이상) 를 묻습니다.
자동화한다면 파이프로도 됩니다.

```bash
printf 'admin\n관리자\n<password>\n<password>\n' | \
  sudo -u claudeweb -H /opt/claude-web/venv/bin/python /opt/claude-web/app.py create-admin
```

### 방법 B — 웹 `/setup` + bootstrap 토큰

관리자가 하나도 없으면 앱이 1회용 토큰을 만들어 로그와 파일에 남깁니다.

```bash
journalctl -u claude-web -n 50 | grep -i "bootstrap token"
# 또는
sudo cat /var/lib/claude-web/setup-token.txt
```

브라우저에서 `http://<서버>/setup` 을 열고 토큰을 입력해 관리자를 만듭니다.
관리자가 생기면 토큰과 파일은 자동으로 사라집니다.

---

## 14. 관리자 초기 설정

브라우저에서 접속 → 로그인 → 우측 상단 아바타 → **관리자**
(또는 직접 `http://<서버>/admin/claude`)

상단 탭: `Dashboard` / `Users` / `Claude 설정` / `System`

### Claude 설정 탭에서 순서대로

| 항목 (화면 그대로) | 넣을 값 |
| --- | --- |
| **Claude 연결 방식** | `cli` |
| **Claude CLI 경로** | `sudo -u claudeweb -H which claude` 결과. 예: `/usr/bin/claude` |
| **Working Directory** | `/var/lib/claude-web/workspace` — 존재하는 디렉터리여야 저장됩니다 |
| **Extra Args** | 보통 비움. 모델을 고정하려면 `--model sonnet` |
| **세션 resume 사용** | 켬 (끄면 매 요청마다 DB 의 최근 대화를 프롬프트에 넣는 방식) |
| **Timeout (초)** | `180` — 올리려면 gunicorn / nginx 도 같이 |
| **최대 동시 실행** | `3` — 서버 사양에 맞게. 세션 단위 lock 은 이와 별개로 항상 동작 |

**저장** 을 누른 뒤 **`Claude 연결 테스트`** 버튼을 누릅니다.

성공하면 이렇게 나옵니다.

```
ok            true
found         true
resolved_path /usr/bin/claude
version       2.1.263 (Claude Code)
authenticated true
reply         OK
```

`authenticated: false` 면 10단계 인증이 서비스 계정에서 안 된 것입니다.
`found: false` 면 CLI 경로가 틀렸습니다.

> API 방식을 쓸 경우: **Claude 연결 방식** 을 `api` 로 바꾸고 **API Key** 를
> 입력합니다. 저장하면 `cryptography` 로 암호화되어 DB 에 들어가고, 이후 화면에는
> 마스킹된 미리보기만 나옵니다. 원문은 다시 표시되지 않고 로그에도 남지 않습니다.

### Users 탭

사용자를 추가합니다. 역할은 `admin` / `user` 두 가지입니다.
비밀번호는 Werkzeug 해시(scrypt)로 저장되며 평문으로 보관되지 않습니다.

---

## 15. nginx

```bash
sudo cp /opt/claude-web/deploy/nginx-http.conf.example \
        /etc/nginx/sites-available/claude-web
sudo nano /etc/nginx/sites-available/claude-web   # server_name 수정
sudo ln -sf /etc/nginx/sites-available/claude-web /etc/nginx/sites-enabled/
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t
sudo systemctl reload nginx
```

핵심 값 두 가지:

```nginx
# 앱의 MAX_CONTENT_LENGTH 와 맞춘다.
#   MAX_UPLOAD_MB(10) x MAX_IMAGES_PER_MESSAGE(5) + 1MB = 51MiB
client_max_body_size 52m;

# Claude(180) < gunicorn(300) < 여기(360)
proxy_read_timeout 360s;
proxy_send_timeout 360s;
```

`client_max_body_size` 가 더 작으면 nginx 가 먼저 413 을 내며, 앱이 주는
"파일이 너무 큽니다 (…/최대 10MB)" 같은 친절한 안내가 안 나옵니다.

### PWA 정적 파일

`/manifest.webmanifest`, `/sw.js`, `/static/*` 는 전부 Flask 가 내보냅니다.
nginx 는 그대로 프록시만 하면 됩니다. `/sw.js` 는 앱이 루트 경로에서
`Service-Worker-Allowed: /` 헤더와 함께 내보내므로 스코프가 사이트 전체입니다.

```bash
curl -sI http://<서버>/sw.js | grep -i 'service-worker-allowed\|content-type'
curl -sI http://<서버>/manifest.webmanifest | grep -i content-type
```

> `/static/` 을 nginx 가 직접 주는 최적화는 **권장하지 않습니다.**
> 앱이 붙이는 보안 헤더가 빠지고, 서비스워커 캐시 버전과 어긋날 수 있습니다.
> 특히 **`/api/attachments/` 는 절대 alias 하지 마세요.** 첨부 이미지는 Flask 가
> 세션 권한을 검사한 뒤에만 내보냅니다. 정적 파일로 노출하면 URL 만 알면
> 누구나 private 대화의 이미지를 볼 수 있게 됩니다.

### HTTPS (사내 인증서)

`deploy/nginx-https.conf.example` 을 쓰세요.

사내 도메인은 외부에서 접근할 수 없어 Let's Encrypt 의 HTTP-01 검증이
보통 불가능합니다. 다음 중 회사 정책에 맞는 것을 쓰세요.

1. **사내 CA 가 발급한 서버 인증서** (권장 — 사내 PC 에 CA 가 이미 배포돼 있음)
2. DNS-01 방식의 공인 인증서 (외부 DNS 를 쓰는 도메인일 때만)

자체 서명 인증서는 브라우저 경고가 뜨고 PWA 설치와 서비스워커가 동작하지
않습니다. 사내 CA 를 쓰거나, 아니면 그냥 HTTP 로 가는 편이 낫습니다.

HTTPS 로 전환하면 `.env` 를 고치고 재시작하세요.

```bash
sudo -u claudeweb sed -i 's/^SESSION_COOKIE_SECURE=.*/SESSION_COOKIE_SECURE=1/' /opt/claude-web/.env
sudo systemctl restart claude-web
```

---

## 16. 방화벽

nginx 만 외부에 열고 gunicorn 은 루프백에만 둡니다.

```bash
sudo ufw status

# 켜서 쓴다면
sudo ufw allow OpenSSH
sudo ufw allow 'Nginx Full'      # 80 + 443. HTTP 만이면 'Nginx HTTP'
sudo ufw enable
sudo ufw status numbered
```

8080 은 열지 않습니다. 실제로 닫혔는지 확인:

```bash
ss -ltnp | grep -E ':(80|443|8080)\s'
# 8080 은 127.0.0.1:8080 이어야 하고 0.0.0.0:8080 이면 안 됩니다
```

---

## 17. 파일 권한 최종 점검

```bash
sudo ls -ld /opt/claude-web /var/lib/claude-web \
            /var/lib/claude-web/uploads /var/lib/claude-web/home
sudo ls -l  /opt/claude-web/.env
sudo ls -l  /var/lib/claude-web/chat.db*
sudo ls -l  /var/lib/claude-web/home/.claude/.credentials.json
```

기대값:

```
drwxr-xr-x  claudeweb claudeweb  /opt/claude-web
drwxr-x---  claudeweb claudeweb  /var/lib/claude-web
drwxr-x---  claudeweb claudeweb  /var/lib/claude-web/uploads
drwx------  claudeweb claudeweb  /var/lib/claude-web/home
-rw-------  claudeweb claudeweb  /opt/claude-web/.env             ← 0600
-rw-------  claudeweb claudeweb  /var/lib/claude-web/chat.db      ← 앱이 0600 으로 맞춤
-rw-------  claudeweb claudeweb  .../.claude/.credentials.json    ← 0600
```

`.env` 에는 SECRET_KEY, DB 에는 Claude API Key 와 비밀번호 해시가 들어갑니다.
일반 사용자가 읽을 수 없어야 합니다. 앱이 기동할 때
`chat.db` / `-wal` / `-shm` 을 0600 으로 다시 맞춥니다.

---

## 18. 배포 후 기능 테스트

브라우저에서 `http://<서버>` 에 접속해 실제로 눌러 봅니다.

- [ ] 1. 로그인 (잘못된 비밀번호 → 오류 메시지 / 정상 → 채팅 화면)
- [ ] 2. 관리자 페이지 4개 탭이 모두 열리는가
- [ ] 3. Users 탭에서 일반 사용자 생성
- [ ] 4. 프로젝트 생성 / 조회
- [ ] 5. 세션 생성
- [ ] 6. private 세션 — 다른 사용자로 로그인해 **목록에 안 보이고 URL 직접 입력도 막히는가**
- [ ] 7. public 세션 — 두 사용자가 같은 세션에서 대화하고 작성자 이름이 구분되는가
- [ ] 8. Claude 에게 질문 → 답변
- [ ] 9. 이어서 한 번 더 질문 → 앞 내용을 기억하는가 (resume)
- [ ] 10. 이미지 업로드 (첨부 버튼 / 드래그앤드롭 / 붙여넣기)
- [ ] 11. Claude 가 이미지 내용을 읽는가
- [ ] 12. 새로고침 / 뒤로가기 후에도 상태 유지
- [ ] 13. 휴대폰에서 접속 → 홈 화면에 추가(PWA)

셸에서 빠르게 확인하려면:

```bash
curl -s http://127.0.0.1:8080/health                     # {"status":"ok"}
curl -sI http://<서버>/ | head -3                         # 302 -> /login
curl -sI http://<서버>/sw.js | grep -i service-worker
sudo -u claudeweb -H claude -p "Respond only with OK"
```

---

## 19. 재부팅 테스트

```bash
sudo reboot
```

다시 올라온 뒤:

```bash
systemctl status nginx
systemctl status claude-web
systemctl is-enabled nginx claude-web         # 둘 다 enabled
journalctl -u claude-web -b -n 50             # 이번 부팅 로그
sudo -u claudeweb -H claude -p "Respond only with OK"
curl -s http://127.0.0.1:8080/health
```

그리고 브라우저로 실제 접속해서 로그인까지 확인합니다.
`SECRET_KEY` 를 `.env` 에 고정해 두었으므로 재부팅해도 로그인이 유지됩니다.
(비워 두면 재시작마다 전원 로그아웃됩니다)

---

## 20. 기존 데이터 이전 (개발 PC → 운영 서버)

`chat.db` 와 `uploads/` **두 개만** 옮기면 사용자 / 프로젝트 / 세션 / 메시지 /
첨부파일이 모두 유지됩니다.

### 옮기기

개발 PC 에서:

```bash
# 앱을 멈춘 뒤
cd claude-web
venv/bin/python app.py backup        # data/backups/ 에 일관된 스냅샷 생성
tar -czf claude-web-data.tar.gz -C data chat.db uploads
```

서버에서:

```bash
sudo systemctl stop claude-web
sudo -u claudeweb tar -xzf claude-web-data.tar.gz -C /var/lib/claude-web
sudo chown -R claudeweb:claudeweb /var/lib/claude-web
sudo -u claudeweb HOME=/var/lib/claude-web/home \
     /opt/claude-web/venv/bin/python /opt/claude-web/app.py migrate
sudo systemctl start claude-web
```

### 첨부파일 경로에 대해

이전 버전(`user_version` 2 이하)은 `attachments.file_path` 에 **절대경로**를
저장했습니다. 그대로 옮기면 업로드 경로가 달라져(`C:\...\data\uploads` →
`/var/lib/claude-web/uploads`) 첨부 API 의 경로 검사에 전부 걸려 이미지가
하나도 열리지 않습니다.

`user_version = 3` 마이그레이션이 이 값들을 `UPLOAD_DIR` 기준 **상대경로**
(`project_1/session_2/<uuid>.png`)로 바꿉니다. 파일 자체는 건드리지 않고 DB 의
표기만 옮깁니다. 이후 새로 올리는 첨부도 상대경로로 저장되므로, 앞으로는
경로가 바뀌어도 그대로 동작합니다.

이전 후 확인:

```bash
sudo -u claudeweb sqlite3 /var/lib/claude-web/chat.db \
  "SELECT id, file_path FROM attachments LIMIT 5;"
# project_1/session_1/xxxx.png  처럼 상대경로여야 합니다
# / 나 C: 로 시작하면 마이그레이션이 안 돌았거나 규칙 밖의 경로입니다
```

그리고 브라우저에서 예전 대화의 이미지를 실제로 열어 보세요.

### 옮기지 않는 것

| | 이유 |
| --- | --- |
| `.env` | 서버마다 경로가 다릅니다. 단 **SECRET_KEY 는 옮기는 편이 낫습니다** — 바꾸면 저장해 둔 Claude API Key 가 복호화되지 않습니다 (CLI 방식만 쓴다면 무관) |
| `venv/` | OS 가 다릅니다. 서버에서 새로 만듭니다 |
| `.claude/` 인증정보 | 서버에서 다시 인증하세요 (10단계) |

---

## 21. 업데이트 배포

```bash
# 1. 반드시 먼저 백업
sudo bash /opt/claude-web/scripts/backup.sh

# 2. 소스 갱신
cd /opt/claude-web
sudo -u claudeweb git pull

# 3. 의존성 (requirements.txt 가 바뀌었을 때만 실제 변화가 생김)
sudo -u claudeweb venv/bin/pip install -r requirements.txt

# 4. 마이그레이션 (변경 없으면 아무것도 안 함. 항상 실행해도 안전)
sudo -u claudeweb HOME=/var/lib/claude-web/home venv/bin/python app.py migrate

# 5. 재시작
sudo systemctl restart claude-web
systemctl status claude-web
journalctl -u claude-web -n 30
```

`deploy/claude-web.service` 나 nginx 설정이 바뀌었으면 추가로:

```bash
sudo cp /opt/claude-web/deploy/claude-web.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl restart claude-web

sudo cp /opt/claude-web/deploy/nginx-http.conf.example /etc/nginx/sites-available/claude-web
# server_name 다시 수정
sudo nginx -t && sudo systemctl reload nginx
```

### UI 를 고쳤는데 옛 화면이 보일 때

서비스워커가 정적 파일을 캐시합니다. `static/shared.css` 나 `static/sw.js` 를
고쳤다면 `static/sw.js` 의 캐시 버전을 올리세요.

```js
const VERSION = "claude-web-v3";   // -> v4
```

`activate` 에서 이전 버전 캐시를 통째로 지웁니다.
HTML / API 응답은 애초에 캐시하지 않으므로(`no-store`) 영향받지 않습니다.

---

## 22. 백업

```bash
sudo bash /opt/claude-web/scripts/backup.sh
# 기본 저장 위치: /var/backups/claude-web  (14일 보관)

sudo BACKUP_ROOT=/mnt/nas/claude-web KEEP=60 bash /opt/claude-web/scripts/backup.sh
```

스크립트가 하는 일:

- `sqlite3 chat.db ".backup ..."` — **단순 `cp` 가 아닙니다.** WAL 모드라
  서비스가 도는 중 `cp` 하면 `-wal` 파일과 어긋난 스냅샷이 나올 수 있습니다.
  online backup 은 실행 중에도 일관된 사본을 만듭니다.
- 백업본에 `PRAGMA integrity_check` 를 돌려 `ok` 인지 확인
- `uploads/` 를 tar.gz 로
- `.env` 사본 (SECRET_KEY 포함)
- 보관 기간이 지난 파일 정리

### 백업 대상 정리

| 대상 | 필수 | 비고 |
| --- | --- | --- |
| `/var/lib/claude-web/chat.db` | ✅ | 사용자·대화·설정 전부 |
| `/var/lib/claude-web/uploads/` | ✅ | 첨부 이미지. DB 만으로는 복구 불가 |
| `/opt/claude-web/.env` | ✅ | SECRET_KEY. 잃으면 API Key 복호화 불가 |
| Claude 인증정보 | ❌ | 아래 참고 |

**Claude 인증정보(`/var/lib/claude-web/home/.claude/.credentials.json`)는
일반 백업에 넣지 마세요.** 장기 유효 자격증명이라, 백업 파일이 있는 곳까지
계정 권한이 번집니다. 서버를 다시 만들 때는 복원하지 말고
`sudo -u claudeweb -H claude auth login` 으로 다시 인증하세요.
(방법 B 의 `CLAUDE_CODE_OAUTH_TOKEN` 도 같은 기준으로, 비밀 관리 체계에
따로 보관합니다)

### 자동 백업

```bash
sudo crontab -e
```

```cron
# 매일 새벽 3시
0 3 * * * /bin/bash /opt/claude-web/scripts/backup.sh >> /var/log/claude-web-backup.log 2>&1
```

### 복구

```bash
sudo systemctl stop claude-web
sudo -u claudeweb cp /var/backups/claude-web/chat-YYYYMMDD-HHMMSS.db \
                     /var/lib/claude-web/chat.db
sudo -u claudeweb rm -f /var/lib/claude-web/chat.db-wal /var/lib/claude-web/chat.db-shm
sudo -u claudeweb tar -xzf /var/backups/claude-web/uploads-YYYYMMDD-HHMMSS.tar.gz \
                     -C /var/lib/claude-web
sudo chown -R claudeweb:claudeweb /var/lib/claude-web
sudo systemctl start claude-web
```

---

## 23. 장애 대응

### 웹이 아예 안 열림

```bash
systemctl status nginx
systemctl status claude-web
ss -ltnp | grep -E ':(80|443|8080)\s'
sudo ufw status
```

### 502 Bad Gateway

앱이 죽었거나 8080 을 안 듣고 있습니다.

```bash
journalctl -u claude-web -n 100 --no-pager
sudo tail -50 /var/log/nginx/claude-web.error.log
curl -s http://127.0.0.1:8080/health
```

자주 나오는 원인:

| 로그에 보이는 것 | 원인 |
| --- | --- |
| `ModuleNotFoundError` | venv 에 설치가 덜 됨 → `pip install -r requirements.txt` |
| `Permission denied: '/var/lib/claude-web/...'` | 소유권 → `chown -R claudeweb:claudeweb /var/lib/claude-web` |
| `Address already in use` | 8080 을 다른 프로세스가 점유 → `ss -ltnp \| grep 8080` |
| `Read-only file system` | `ProtectSystem=strict` 범위 밖에 쓰려 함 → `ReadWritePaths` 확인 |

### 504 Gateway Time-out

타임아웃 3단이 어긋났습니다. Claude(180) < gunicorn(300) < nginx(360) 확인.

```bash
grep -i timeout /etc/nginx/sites-available/claude-web
grep -i GUNICORN_TIMEOUT /opt/claude-web/.env
sudo -u claudeweb sqlite3 /var/lib/claude-web/chat.db \
  "SELECT value FROM settings WHERE key='claude_timeout';"
```

### Claude 호출만 실패

```bash
sudo -u claudeweb -H claude -p "Respond only with OK"
sudo -u claudeweb -H claude auth status --text
sudo -u claudeweb -H which claude
sudo systemctl show claude-web -p Environment      # HOME / PATH 확인
```

| 증상 | 원인 |
| --- | --- |
| 셸에서는 되는데 웹에서만 실패 | unit 의 `HOME` 또는 `PATH`. 대부분 이것입니다 |
| `Claude CLI 를 찾을 수 없습니다` | 관리자 페이지의 CLI 경로가 틀림 → `which claude` 결과로 교체 |
| `authenticated: false` | 서비스 계정에서 인증이 안 됨 → 10단계 다시 |
| `시간 초과(180초)` | 질문이 무겁거나 네트워크. Timeout 을 올리고 gunicorn/nginx 도 함께 |
| `Working Directory 경로가 올바르지 않습니다` | 그 디렉터리가 없음. 앱이 저장 시점에 검사하므로 보통 사전에 걸립니다 |

### DB 오류

```bash
ls -l /var/lib/claude-web/chat.db*
sudo -u claudeweb sqlite3 /var/lib/claude-web/chat.db "PRAGMA integrity_check;"
df -h /var/lib
```

- `unable to open database file` → 부모 디렉터리 쓰기 권한. WAL 때문에
  디렉터리에도 쓸 수 있어야 합니다
- `database is locked` → 15초 busy_timeout 이 있으므로 보통 안 납니다.
  나온다면 다른 프로세스가 DB 를 잡고 있는지 확인
- `disk I/O error` → 디스크 확인

### 이미지 업로드 실패

| 증상 | 확인 |
| --- | --- |
| 413 (nginx 기본 오류 페이지) | `client_max_body_size` 가 51MiB 보다 작음 |
| "파일이 너무 큽니다" (앱 메시지) | 정상. `MAX_UPLOAD_MB` 한도 |
| "이미지 파일이 아닙니다" | 확장자와 실제 내용 불일치. 정상 동작 |
| 500 | `ls -ld /var/lib/claude-web/uploads` 권한, `df -h` 디스크 |

```bash
sudo -u claudeweb touch /var/lib/claude-web/uploads/.t && \
sudo -u claudeweb rm /var/lib/claude-web/uploads/.t && echo "쓰기 OK"
```

### 첨부 이미지가 403/404

DB 의 `file_path` 가 절대경로로 남아 있을 수 있습니다. (20단계 참고)

```bash
sudo -u claudeweb sqlite3 /var/lib/claude-web/chat.db \
  "SELECT id, file_path FROM attachments WHERE file_path LIKE '/%' OR file_path LIKE '_:%';"
# 결과가 있으면
sudo -u claudeweb HOME=/var/lib/claude-web/home \
     /opt/claude-web/venv/bin/python /opt/claude-web/app.py migrate
```

### 로그인이 안 되거나 자꾸 풀림

- `SECRET_KEY` 가 `.env` 에 비어 있으면 재시작마다 전원 로그아웃됩니다
- HTTP 로 서비스하는데 `SESSION_COOKIE_SECURE=1` 이면 브라우저가 쿠키를
  저장하지 않아 로그인이 되지 않습니다
- 연속 실패로 잠긴 경우 기본 10분 뒤 풀립니다 (`LOGIN_WINDOW_MINUTES`)

---

## 24. 디스크 점검

업로드 이미지와 SQLite 가 계속 늘어납니다.

```bash
df -h /var/lib /var/log
du -sh /var/lib/claude-web
du -sh /var/lib/claude-web/uploads
du -sh /var/lib/claude-web/backups
ls -lh /var/lib/claude-web/chat.db*
journalctl --disk-usage
```

정리:

```bash
# systemd journal 을 30일치만 유지
sudo journalctl --vacuum-time=30d

# 자동 마이그레이션 백업은 최근 10개만 남습니다 (앱이 정리)
ls -lt /var/lib/claude-web/backups | head
```

프로젝트나 세션을 지우면 해당 업로드 폴더도 함께 지워집니다.
오래된 대화를 관리자 페이지에서 정리하는 것이 가장 확실한 감량 방법입니다.

---

## 25. 서비스 운영 명령 요약

```bash
sudo systemctl start   claude-web
sudo systemctl stop    claude-web
sudo systemctl restart claude-web
sudo systemctl reload  claude-web     # gunicorn 에 HUP (워커만 재기동)
systemctl status       claude-web
systemctl is-enabled   claude-web

journalctl -u claude-web -f
journalctl -u claude-web -n 200 --no-pager
journalctl -u claude-web --since today
journalctl -u claude-web -p err        # 오류만

sudo systemctl reload nginx
sudo nginx -t
sudo tail -f /var/log/nginx/claude-web.error.log

# 관리 명령
cd /opt/claude-web
sudo -u claudeweb HOME=/var/lib/claude-web/home venv/bin/python app.py migrate
sudo -u claudeweb HOME=/var/lib/claude-web/home venv/bin/python app.py backup
sudo -u claudeweb HOME=/var/lib/claude-web/home venv/bin/python app.py create-admin

# Claude 점검
sudo -u claudeweb -H claude --version
sudo -u claudeweb -H claude auth status --text
sudo -u claudeweb -H claude -p "Respond only with OK"
sudo -u claudeweb -H claude doctor
```

---

## 부록 — 설치 체크리스트

```
[ ]  1. OS 확인 (Ubuntu 20.04+, 4GB+, 디스크 여유)
[ ]  2. apt 패키지 (git python3 python3-venv python3-pip nginx sqlite3 curl ca-certificates gnupg)
[ ]  3. 서비스 계정 claudeweb (home=/var/lib/claude-web/home, shell=/bin/bash)
[ ]  4. 소스 -> /opt/claude-web, chown claudeweb
[ ]  5. 데이터 디렉터리 + 권한 + 쓰기 테스트
[ ]  6. venv + pip install -r requirements.txt + import 확인
[ ]  7. .env 작성, SECRET_KEY 생성, chmod 600
[ ]  8. app.py migrate -> user_version 3 / journal_mode wal
[ ]  9. Claude CLI 설치, claude --version
[ ] 10. ★ sudo -u claudeweb -H claude -p "Respond only with OK" 가 OK
[ ] 11. 외부 통신 확인 (필요시에만 프록시)
[ ] 12. systemd 등록 + enable + start + status
[ ] 13. 관리자 계정 생성
[ ] 14. /admin/claude 설정 + 연결 테스트 ok=true
[ ] 15. nginx 설정 + nginx -t + reload
[ ] 16. 방화벽 80/443 만, 8080 은 127.0.0.1 바인딩 확인
[ ] 17. 파일 권한 점검 (.env 0600, chat.db 0600, credentials 0600)
[ ] 18. 브라우저 기능 테스트 13항목
[ ] 19. 재부팅 테스트
[ ] 20. 백업 1회 실행 + cron 등록
```
