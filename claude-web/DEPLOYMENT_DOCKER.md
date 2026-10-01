# claude-web Docker 배포 가이드

사내 Linux 서버에 **Docker 로** 배포하는 절차다. OS 에는 Docker 와 git 만 설치하고,
Python / gunicorn / Claude Code CLI 는 컨테이너 이미지 안에 둔다.

> OS 에 Python 과 Claude CLI 를 직접 설치하는 방식은 [DEPLOYMENT.md](DEPLOYMENT.md) 를 본다.
> 두 방식은 같은 소스, 같은 DB 스키마, 같은 데이터 경로를 쓴다. 서로 오갈 수 있다.

> **운영 서버(nfs-181)를 실제로 다시 설치할 때는
> [DEPLOYMENT_SERVER_NFS181.md](DEPLOYMENT_SERVER_NFS181.md) 를 본다.**
> 이 문서는 일반 가이드이고, 그 문서에는 그 서버의 실제 값(포트 19780,
> 데이터 경로, 기존 podman 과의 공존, 실제로 겪은 장애와 조치)이 들어 있다.

---

## 0. 이번 Docker 화의 핵심

**세 가지의 lifecycle 을 완전히 분리한다.**

| 무엇 | 어디에 | 바뀌면 |
|---|---|---|
| 웹 소스 | host 의 git 작업 트리 `/opt/claude-web` | `git pull` + `docker compose restart app` |
| 실행환경 | Docker 이미지 `claude-web-runtime:1.0.0` | `docker compose build app` + `up -d app` |
| DB / 업로드 | host 디렉터리 `/var/lib/claude-web` | 컨테이너를 지워도 남는다 |
| Claude 인증 | named volume `claude-web-home` | 컨테이너를 지워도 남는다 |

즉 **평소 코드 수정에는 이미지를 다시 만들지 않는다.**
Python 패키지 / Claude CLI / OS 패키지가 바뀔 때만 이미지를 새로 만든다.

### 최종 구조

```text
  브라우저 (사내 PC)
        │  http://<사내 호스트명>/
        ▼
  ┌─────────────────────────────── HOST ───────────────────────────────┐
  │                                                                    │
  │  /opt/claude-web                     <- git clone (repo 루트)       │
  │  /opt/claude-web/claude-web          <- compose 프로젝트 디렉터리    │
  │      app.py  templates/  static/  deploy/  docker/  compose.yml    │
  │      .env                            <- 서버 설정 + 이미지 태그      │
  │                                                                    │
  │  /var/lib/claude-web                 <- 데이터 (git 밖)             │
  │      chat.db  chat.db-wal  uploads/  notes/  workspace/  backups/  │
  │                                                                    │
  │  docker volume claude-web-home       <- Claude 인증 / 세션 히스토리  │
  │                                                                    │
  │  ┌── docker compose (project: claude-web) ───────────────────────┐ │
  │  │                                                              │ │
  │  │  init   (1회 실행 후 종료)  데이터 디렉터리 생성 + chown        │ │
  │  │                                                              │ │
  │  │  nginx  :80 --------------> app:8080                         │ │
  │  │    └ 외부에 열리는 유일한 포트                                 │ │
  │  │                                                              │ │
  │  │  app    gunicorn(1 worker / 8 threads) + Flask               │ │
  │  │    ├ /app                 <- bind mount, 읽기 전용            │ │
  │  │    ├ /var/lib/claude-web  <- bind mount, 읽기/쓰기            │ │
  │  │    ├ /home/claudeweb      <- named volume (Claude 인증)       │ │
  │  │    └ /usr/bin/claude  ---- 사외 HTTPS ---> api.anthropic.com  │ │
  │  └──────────────────────────────────────────────────────────────┘ │
  └────────────────────────────────────────────────────────────────────┘
```

### 이미지에 들어가는 것 / 들어가지 않는 것

| 이미지 안 (`claude-web-runtime`) | 이미지 밖 |
|---|---|
| Python 3.12 (Debian 13 slim) | app.py, templates/, static/, deploy/ … (bind mount) |
| Flask, Werkzeug, python-dotenv, cryptography | `.env` (compose `env_file` + dotenv) |
| gunicorn | `deploy/gunicorn.conf.py` (bind mount -> 재빌드 없이 튜닝) |
| Claude Code CLI (`/usr/bin/claude`) | Claude 인증정보 (named volume) |
| ca-certificates, curl | chat.db, uploads/, workspace/ (host 디렉터리) |
| 실행 사용자 `claudeweb` (uid/gid 1000) | nginx 설정 (bind mount -> 재빌드 없이 수정) |

이미지에 **소스를 COPY 하지 않는다.** build context 에서 이미지로 들어가는 파일은
`requirements.txt` 와 `docker/entrypoint.sh` 딱 두 개다.

---

## 1. 사전 확인

```bash
cat /etc/os-release
uname -a
df -h /var /opt
```

필요한 것은 네 가지뿐이다.

| 항목 | 이유 |
|---|---|
| 인터넷(HTTPS) 나가는 경로 | Claude 호출(`api.anthropic.com`), 이미지/패키지 다운로드 |
| 디스크 여유 | 이미지가 디스크에서 약 600MB (반입용 tar.gz 는 약 170MB) + DB/업로드. `docker system df` 로 확인 |
| 사내 호스트명 | 브라우저 접속 주소. 사내 DNS 에서 조회되어야 한다 |
| Claude 계정 | Pro / Max / Team / Enterprise 중 하나 (무료 요금제는 Claude Code 불가) |

인터넷이 막혀 있으면 [6-B 이미지 반입](#6-b-미리-만든-이미지를-반입한다-인터넷-제한-환경) 을 쓴다.
단 **Claude 호출 자체는 인터넷이 필요하다.** 이건 우회할 수 없다.

---

## 2. Docker Engine + Compose 설치

### Rocky Linux / RHEL / CentOS Stream 9

```bash
# 배포판 기본 컨테이너 도구가 있으면 먼저 지운다 (Docker 공식 문서 요구사항)
#
# 주의: 이 서버에서 **다른 시스템이 podman 을 쓰고 있다면 podman 을 지우면 안 된다.**
#       그때는 아래 목록에서 podman / runc 를 빼고, docker 라는 이름의 래퍼를
#       심는 podman-docker 패키지만 지운다.  ->  dnf remove -y podman-docker
#       (실제 사례: DEPLOYMENT_SERVER_NFS181.md 3절)
dnf remove -y docker docker-client docker-client-latest docker-common \
              docker-latest docker-latest-logrotate docker-logrotate \
              docker-engine podman runc

dnf -y install dnf-plugins-core
dnf config-manager --add-repo https://download.docker.com/linux/centos/docker-ce.repo
dnf -y install docker-ce docker-ce-cli containerd.io \
               docker-buildx-plugin docker-compose-plugin
```

- Rocky 는 RHEL/CentOS 호환 재빌드라 위 `centos` 저장소를 쓴다.
  받아지지 않으면 `https://download.docker.com/linux/rhel/docker-ce.repo` 로 바꾼다.
- GPG 키 지문을 물으면 `060A 61C5 1B55 8A7F 742B 77AA C52F EB6B 621E 9F35` 인지 확인한다.

### Ubuntu / Debian

```bash
apt-get update
apt-get install -y ca-certificates curl gnupg
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
  -o /etc/apt/keyrings/docker.asc
chmod a+r /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] \
https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
  > /etc/apt/sources.list.d/docker.list
apt-get update
apt-get install -y docker-ce docker-ce-cli containerd.io \
                   docker-buildx-plugin docker-compose-plugin
```

### 공통 : 기동 + 확인

```bash
systemctl enable --now docker
systemctl is-enabled docker        # enabled  <- 재부팅 후 자동 시작
docker --version                   # Docker version 2x.x.x
docker compose version             # Docker Compose version v2.x / v5.x
docker run --rm hello-world
```

`systemctl enable` 이 켜져 있고 compose 의 `restart: unless-stopped` 가 붙어 있으면
**재부팅 후 서비스가 자동으로 다시 올라온다. 별도의 systemd unit 은 만들지 않는다.**
(→ [17. 재부팅](#17-재부팅))

---

## 3. git 설치 + 소스 clone

```bash
# Rocky/RHEL
dnf -y install git
# Ubuntu/Debian
# apt-get install -y git

git --version

git clone https://github.com/readersun/chat-bot-v1.git /opt/claude-web
cd /opt/claude-web/claude-web        # <- 앞으로 모든 명령은 여기서 실행한다
ls compose.yml docker/Dockerfile     # 둘 다 보여야 한다
```

> **작업 디렉터리 주의**
> repo 루트는 `/opt/claude-web`, 애플리케이션과 `compose.yml` 은 그 아래
> `claude-web/` 에 있다. compose 명령은 항상 `/opt/claude-web/claude-web` 에서 실행한다.
> `.env` 도 이 디렉터리에 둔다. (앱과 compose 가 같은 파일을 읽는다)
>
> 매번 타이핑하기 번거로우면 셸 설정에 별칭을 넣어 둔다.
> ```bash
> echo "alias cw='cd /opt/claude-web/claude-web'" >> ~/.bashrc
> ```

---

## 4. 데이터 디렉터리

DB 와 업로드는 **git 작업 트리 밖**에 둔다. `git pull` 이나 컨테이너 재생성과 무관해진다.

```bash
install -d -m 0750 /var/lib/claude-web
```

하위 디렉터리(`uploads`, `workspace`, `backups`)와 소유권은 `init` 컨테이너가 처음
기동할 때 자동으로 만들고 `claudeweb`(uid 1000) 으로 맞춘다. 직접 만들 필요는 없다.

이미 uid 1000 인 다른 사용자가 있어 소유권을 넘기고 싶지 않다면 `.env` 에서
`APP_UID` / `APP_GID` 를 그 서버에 맞게 정하고 이미지를 다시 빌드한다.

```bash
# (선택) 전용 host 계정을 만들고 그 UID 로 이미지를 빌드하는 경우
groupadd -r claudeweb
useradd -r -g claudeweb -M -s /sbin/nologin claudeweb
id -u claudeweb          # 이 값을 .env 의 APP_UID 에 넣는다
```

---

## 5. `.env` 작성

```bash
cd /opt/claude-web/claude-web
cp .env.example .env
chmod 600 .env
python3 -c "import secrets;print(secrets.token_hex(32))"   # SECRET_KEY 생성
# python3 이 없으면: docker run --rm python:3.12-slim python -c "import secrets;print(secrets.token_hex(32))"
vi .env
```

`.env` 는 `root:root 0600` 으로 두면 된다. 컨테이너는 이 파일을 직접 읽지 않는다.
compose 의 `env_file` 이 값을 읽어서(host 의 root 권한으로) 컨테이너 환경변수로
넣어주기 때문이다. 앱도 `.env` 를 못 읽으면 환경변수만 쓰고 한 줄 남기고 넘어간다.

컨테이너가 파일도 직접 읽게 하고 싶다면 실행 사용자(uid 1000)가 읽을 수 있어야 한다.

```bash
chown root:1000 .env && chmod 640 .env     # root 만 수정, 컨테이너는 읽기만
```

운영에서 반드시 확인/수정할 값:

```ini
# --- Docker (compose 가 읽는다) ---
CLAUDE_WEB_IMAGE=claude-web-runtime:1.0.0
NGINX_IMAGE=nginx:1.29-alpine
HTTP_PORT=80
HOST_DATA_DIR=/var/lib/claude-web

# --- 저장 위치 (컨테이너 안 경로. 바꾸지 말 것) ---
DATABASE_PATH=/var/lib/claude-web/chat.db
UPLOAD_DIR=/var/lib/claude-web/uploads
BACKUP_DIR=/var/lib/claude-web/backups

# --- 키 / 쿠키 ---
SECRET_KEY=<위에서 만든 64자 hex>
SESSION_COOKIE_SECURE=0      # 평문 HTTP 면 0. 1 로 두면 로그인이 안 된다
TRUST_PROXY=1                # nginx 를 앞에 두므로 1

# --- Claude 초기값 (최초 1회만 쓰인다) ---
CLAUDE_PROVIDER=cli
CLAUDE_BIN=/usr/bin/claude
CLAUDE_WORKDIR=/var/lib/claude-web/workspace
CLAUDE_TIMEOUT=180
MAX_CONCURRENT_CLAUDE=3
```

### host 의 80 을 이미 다른 웹서버가 쓰고 있다면

`HTTP_PORT` 만 바꾼다. `docker/nginx.conf` 는 고치지 않는다. 컨테이너 **내부** nginx 는
계속 80 을 듣고, host 쪽 publish 포트만 바뀐다.

```ini
HTTP_PORT=19780
```

```bash
firewall-cmd --permanent --add-port=19780/tcp    # --add-service=http 대신
firewall-cmd --reload
```

접속 주소는 `http://<호스트명>:19780/` 이 되고, 확인 명령에도 포트를 붙인다.
비표준 포트에서도 `/admin` 같은 절대 리다이렉트가 깨지지 않도록 nginx 가 `Host` 를
`$http_host`(포트 포함)로 넘긴다. 32768 이상은 ephemeral 포트 범위와 겹치므로 피한다.

기존 웹서버를 그대로 앞단으로 쓰는 쪽이 낫다면
[21. Option 비교](#21-nginx-를-컨테이너로-vs-host-에) 의 Option B 로 간다.

### 설정이 두 곳에 나뉘어 있다 (이 구조를 유지한다)

| 어디 | 무엇 | 반영 시점 |
|---|---|---|
| `.env` | 서버 고정값 — 저장 경로, 포트, SECRET_KEY, 쿠키, 업로드 제한, 로그인 정책, 이미지 태그 | 컨테이너 재시작 |
| 관리자 페이지 → DB `settings` 테이블 | Claude 연결 — provider, CLI 경로, workdir, timeout, 동시 실행 | **즉시** (재시작 불필요) |

`.env` 의 `CLAUDE_*` 값은 **settings 테이블이 비어 있는 최초 1회**만 초기값으로
쓰인다. 그 뒤로는 관리자 페이지 값이 우선이고, `.env` 를 고쳐도 반영되지 않는다.
**이미지를 재빌드해도 DB 의 settings 는 그대로 남는다.**

주의할 점 두 가지:

- **Compose 의 `env_file` 은 값 뒤의 `#` 을 주석으로 보지 않는다.**
  `CLAUDE_TIMEOUT=180   # 초` 라고 쓰면 값이 `180   # 초` 가 된다. 주석은 줄 단위로만.
- **`SECRET_KEY` 는 한 번 정하면 바꾸지 않는다.** 바꾸면 전원 로그아웃되고,
  관리자 화면에 저장한 Claude API Key 가 이 값에서 파생한 키로 암호화돼 있어
  복호화되지 않는다.

---

## 6. 실행환경 이미지 준비

두 가지 방법 중 하나를 쓴다.

### 6-A. 서버에서 직접 빌드한다 (권장, 인터넷 가능할 때)

```bash
cd /opt/claude-web/claude-web
docker compose build
docker images claude-web-runtime
```

빌드가 하는 일:

1. `python:3.12-slim-trixie` 기반
2. `ca-certificates curl gnupg` 설치
3. Claude Code 공식 apt 저장소 등록 — **서명키 지문을 검증한다.**
   `31DDDE24DDFAB679F42D7BD2BAA929FF1A7ECACE` 와 다르면 빌드가 그 자리에서 실패한다.
4. `claude-code` 설치 → `/usr/bin/claude`, 빌드 중 `claude --version` 으로 확인
5. `gnupg` 제거 (런타임에 불필요)
6. `requirements.txt` 로 pip 설치 + import 검증
7. `claudeweb` 사용자 생성 (uid/gid 기본 1000)

빌드 결과 확인:

```bash
docker run --rm claude-web-runtime:1.0.0 sh -c \
  'python -V; which claude; claude --version; gunicorn --version'
```

검증된 출력 예:

```text
Python 3.12.14
/usr/bin/claude
2.1.280 (Claude Code)
gunicorn (version 26.2.0)
```

### 6-B. 미리 만든 이미지를 반입한다 (인터넷 제한 환경)

인터넷이 되는 PC/서버에서:

```bash
git clone https://github.com/readersun/chat-bot-v1.git
cd chat-bot-v1/claude-web
docker build -f docker/Dockerfile -t claude-web-runtime:1.0.0 .
docker save claude-web-runtime:1.0.0 | gzip > claude-web-runtime-1.0.0.tar.gz
sha256sum claude-web-runtime-1.0.0.tar.gz          # 반입 후 대조용
```

파일을 운영 서버로 옮긴 뒤:

```bash
sha256sum claude-web-runtime-1.0.0.tar.gz          # 값이 같은지 확인
gunzip -c claude-web-runtime-1.0.0.tar.gz | docker load
docker images claude-web-runtime
```

`.env` 의 `CLAUDE_WEB_IMAGE` 가 반입한 태그와 같으면 `docker compose up -d` 는
빌드하지 않고 이 이미지를 쓴다. (`docker compose up` 은 `build:` 가 있어도 이미지가
이미 있으면 빌드하지 않는다. 강제로 빌드할 때만 `--build` 를 준다)

### 이미지 태그를 쓰는 이유

`latest` 에 의존하지 않는다. **소스 버전과 실행환경 버전은 독립이다.**

```text
소스      : git commit 6b94b7d
실행환경  : claude-web-runtime:1.0.0
```

Python 패키지와 Claude CLI 가 그대로면 이미지는 손대지 않고 소스만 `git pull` 한다.
이미지를 올릴 때는 새 태그(`1.0.1`)를 만들고 `.env` 의 한 줄만 바꾼다.
문제가 생기면 그 한 줄을 되돌려 이전 이미지로 즉시 복귀할 수 있다.

컨테이너의 Claude CLI 는 `DISABLE_AUTOUPDATER=1` 로 자동 업데이트를 껐다.
그래서 **이미지 태그 = Claude CLI 버전**이 항상 성립한다. Claude CLI 를 올리려면
이미지를 다시 빌드한다. (컨테이너 안에서 `claude update` 를 쓰면 새 버전이 HOME
볼륨에 깔려 이미지보다 우선하게 되고, 같은 태그가 서버마다 다르게 동작한다)

---

## 7. 기동

```bash
cd /opt/claude-web/claude-web
docker compose up -d
docker compose ps
```

```text
NAME                 SERVICE   STATUS
claude-web-app-1     app       Up (healthy)
claude-web-init-1    init      Exited (0)      <- 정상. 1회 실행 후 종료하는 서비스다
claude-web-nginx-1   nginx     Up (healthy)
```

```bash
docker compose logs app | tail -30
```

처음 기동 로그에서 확인할 것:

```text
[entrypoint] mkdir /var/lib/claude-web/uploads
[entrypoint] mkdir /var/lib/claude-web/workspace
[entrypoint] mkdir /var/lib/claude-web/backups
[entrypoint] chown -R 1000:1000 /var/lib/claude-web
[entrypoint] exec as claudeweb(1000:1000): true
...
[INFO] Starting gunicorn 26.2.0
[INFO] Listening at: http://0.0.0.0:8080
[INFO] Using worker: gthread
[INFO] Booting worker with pid: 7          <- 워커는 1개여야 한다
```

동작 확인:

```bash
curl -fsS http://127.0.0.1/health          # {"status":"ok"}
curl -fsS http://127.0.0.1/healthz         # ok   (nginx 자체 응답)
docker compose exec app id                 # uid=1000(claudeweb)  <- root 가 아니어야 한다
docker compose top app                     # docker-init -> gunicorn master -> worker 1개
```

`docker compose exec` 가 `claudeweb` 으로 실행되는 것이 중요하다. root 로 실행되면
Claude 인증 파일과 SQLite WAL 파일이 root 소유로 만들어져 웹이 읽거나 쓸 수 없게 된다.
그래서 `app` 서비스는 처음부터 `claudeweb` 으로 돌고, 권한 정리는 `init` 서비스가 한다.

### 워커를 1개로 두는 이유 (건드리지 말 것)

앱이 다음 세 가지를 **프로세스 메모리**에 들고 있다.

| 상태 | 역할 |
|---|---|
| `app.ConcurrencyLimiter` | 전체 동시 Claude 실행 수 제한 (`max_concurrent_claude`) |
| `app._SESSION_LOCKS` | 같은 세션 동시 요청 차단 (public 세션 문맥 보호) |
| `auth._setup_token` | 최초 관리자 bootstrap 토큰 |

gunicorn 워커는 서로 다른 프로세스라서 이 상태가 공유되지 않는다. 워커를 N 개로
늘리면 동시 실행 제한이 실질 N 배가 되고, 같은 public 세션에 두 사람이 동시에 보낼 때
세션 lock 을 통과해 `claude --resume` 문맥이 섞이고, `/setup` 토큰이 워커마다 달라진다.

그래서 **워커 1개 + 스레드 8개**(`gthread`)로 동시성을 낸다. 개발 서버
(`app.run(threaded=True)`)와 동작이 정확히 같다. 로그인 rate limit 과 설정값은
SQLite 에 있으므로 영향이 없다. 사용자 수십 명 규모 사내 도구에서는 이 구성으로 충분하다.

설정 파일은 마운트된 소스 `deploy/gunicorn.conf.py` 다. 스레드/타임아웃만 바꾸는 데는
이미지 재빌드가 필요 없다. (`.env` 의 `GUNICORN_THREADS`, `GUNICORN_TIMEOUT`)

### 타임아웃 사슬

```text
Claude timeout 180초  <  gunicorn timeout 300초  <  nginx proxy_read_timeout 360초
(관리자 페이지)          (deploy/gunicorn.conf.py)   (docker/nginx.conf)
```

순서가 뒤집히면 앱이 만든 한글 "시간 초과" 메시지 대신 502/504 가 뜬다.
관리자 페이지에서 Claude timeout 을 300 이상으로 올리려면 나머지 둘도 같이 올린다.

### 업로드 크기

```text
MAX_UPLOAD_MB(10) x MAX_IMAGES_PER_MESSAGE(5) x 1MiB + 1MiB = 51MiB
  -> nginx client_max_body_size 52m
```

`.env` 에서 저 두 값을 올리면 `docker/nginx.conf` 의 `client_max_body_size` 도
같이 올린 뒤 `docker compose restart nginx` 한다. nginx 쪽이 작으면 앱의 친절한
오류 메시지 대신 nginx 의 413 페이지가 뜬다.

---

## 8. Claude 최초 인증 (가장 중요)

컨테이너 안에서 Claude 에 로그인한다. 인증정보는 `/home/claudeweb/.claude/.credentials.json`
(0600) 에 저장되고, 이 경로는 named volume `claude-web-home` 이라서 **컨테이너를
재생성하거나 이미지를 바꿔도 남는다.**

```bash
cd /opt/claude-web/claude-web
docker compose exec app claude
```

1. Claude Code 가 로그인 URL 을 보여준다. 컨테이너에는 브라우저가 없으므로
   `c` 를 눌러 URL 을 복사하거나 화면의 URL 을 그대로 읽어서
   **자기 PC 브라우저**에 붙여 넣는다.
2. 브라우저에서 사내 Claude 계정(Pro/Max/Team/Enterprise)으로 로그인한다.
3. 브라우저가 로그인 코드를 보여주면, 터미널의
   `Paste code here if prompted` 에 붙여 넣는다.
   (컨테이너·SSH 환경에서는 브라우저가 컨테이너의 콜백 포트에 닿지 못해 이 화면이 나온다)
4. `Login successful` 이 뜨면 Enter, 그리고 `/exit` 로 나온다.

### 인증 확인 — 이 단계를 건너뛰지 말 것

```bash
docker compose exec app claude -p "Respond only with OK"
```

`OK` 가 나와야 한다. 인증이 안 된 상태에서는 이렇게 나온다.

```text
Not logged in · Please run /login
```

인증 파일과 진단:

```bash
docker compose exec app ls -l /home/claudeweb/.claude/.credentials.json   # -rw------- claudeweb
docker compose exec app claude doctor
```

`claude doctor` 는 `Path: /usr/bin/claude`, `Search: OK (bundled)` 를 보여준다.
`~/.local/bin` 관련 경고는 apt 설치에서 정상이며 무시해도 된다.

### 인증이 유지되는지 확인

```bash
docker compose down
docker compose up -d
docker compose exec app claude -p "Respond only with OK"     # 여전히 OK
```

`docker compose down` 은 컨테이너만 지우고 볼륨은 지우지 않는다.
**`docker compose down -v` 는 볼륨까지 지운다. 운영에서 쓰지 말 것.**

### 브라우저 붙여넣기가 불가능한 환경이라면

1년짜리 OAuth 토큰을 쓰는 방법도 있다. 인터넷 되는 다른 머신에서

```bash
claude setup-token
```

을 실행해 토큰을 받아 `.env` 에 넣는다. (`.env` 는 0600, git 에 커밋되지 않는다)

```ini
CLAUDE_CODE_OAUTH_TOKEN=<발급받은 토큰>
```

`docker compose up -d app` 으로 반영한다. 이 방식은 모델 호출만 가능하고
토큰이 `.env` 평문에 남으므로, 가능하면 위의 대화형 로그인을 쓴다.

---

## 9. 최초 관리자 계정

두 가지 방법 중 하나. **셸에서 만드는 쪽이 간단하다.**

### 방법 A — 셸 (권장)

```bash
docker compose exec app python app.py create-admin
```

아이디 / 표시 이름 / 비밀번호(8자 이상)를 묻는다.
비밀번호는 `scrypt` 해시로 저장되며 평문으로 남지 않는다.

### 방법 B — 브라우저 `/setup`

관리자가 없으면 앱이 1회용 bootstrap 토큰을 만들어 파일에 남긴다.

```bash
docker compose exec app cat /var/lib/claude-web/setup-token.txt
```

브라우저에서 `http://<사내 호스트명>/setup` 을 열고 이 토큰과 계정 정보를 입력한다.
관리자가 만들어지면 토큰 파일은 자동으로 삭제되고 `/setup` 은 닫힌다.

---

## 10. 관리자 초기 설정

브라우저에서 `http://<사내 호스트명>/` → 로그인 → 우측 상단 **관리자**.

### Claude 설정 (`/admin/claude`)

| 항목 | 넣을 값 | 근거 |
|---|---|---|
| Claude 연결 방식 | `cli` | 구독 계정을 쓰므로 CLI |
| Claude CLI 경로 | `/usr/bin/claude` | `docker compose exec app which claude` 결과 |
| Working Directory | `/var/lib/claude-web/workspace` | 존재하는 디렉터리여야 저장된다 |
| Extra Args | (비움) | 필요할 때만. 예 `--model sonnet` |
| 세션 resume 사용 | 켬 | `claude --resume` 으로 대화 문맥 유지 |
| Timeout (초) | `180` | gunicorn 300 / nginx 360 보다 작아야 한다 |
| 최대 동시 실행 | `3` | 워커가 1개이므로 이 값이 서버 전체 한도다 |

저장 후 **연결 테스트(Connection Test)** 를 누른다. 기대 결과:

```text
ok            true
cli_path      /usr/bin/claude
resolved_path /usr/bin/claude
version       2.1.280 (Claude Code)
authenticated true
reply         OK
```

`found: false` 면 CLI 경로가 틀렸고, `authenticated: false` 면 [8. Claude 인증](#8-claude-최초-인증-가장-중요)
이 끝나지 않았다.

### 사용자 (`/admin/users`)

사용할 사람들의 계정을 만든다. 역할은 `user`, 초기 비밀번호는 본인이 바꾸게 안내한다.
관리자는 시스템 운영 권한만 가지며, **다른 사람의 private 세션 내용은 볼 수 없다.**

### 프로젝트

일반 화면에서 프로젝트를 만들고, 그 아래에 세션을 만든다.
세션이 권한 단위다. `private` 은 소유자만, `public` 은 로그인한 모두가 볼 수 있다.

---

## 11. 기능 테스트 체크리스트

브라우저에서 실제로 확인한다. (`http://<사내 호스트명>/`)

| # | 항목 | 확인 |
|---|---|---|
| 1 | 로그인 / 로그아웃 | |
| 2 | 관리자 페이지 5개 탭 (dashboard / users / claude / system / storage) | |
| 3 | 사용자 생성 → 그 계정으로 로그인 | |
| 4 | 프로젝트 생성 / 이름 변경 (한글) | |
| 5 | 세션 생성 (private) | |
| 6 | 세션 생성 (public) → 다른 계정에서 보이는지 | |
| 7 | 다른 사람의 private 세션 URL 직접 입력 → 접근 거부 | |
| 8 | Claude 에게 질문 → 답변 | |
| 9 | 같은 세션에서 이어 질문 → 앞 대화를 기억하는지 (`--resume`) | |
| 10 | 이미지 업로드 (png/jpg/webp/gif) | |
| 11 | 업로드한 이미지에 대해 질문 → Claude 가 내용을 읽는지 | |
| 12 | 새로고침 후 대화 유지 | |
| 13 | 모바일 브라우저에서 접속 / 화면 동작 | |
| 14 | 메모 생성 (제목 / 내용 / 공개 범위 private) | |
| 15 | 메모에 파일 첨부 (png / pdf / txt) → 다시 열기 | |
| 16 | 메모 수정 / 첨부 추가 / 첨부 삭제 / 메모 삭제 | |
| 17 | public 메모 → 다른 계정에서 "공유 메모" 에 보이는지 | |
| 18 | 다른 사람의 private 메모 URL·첨부 URL 직접 입력 → 접근 거부 | |
| 19 | 남의 public 메모 수정/삭제 시도 → 차단 | |
| 20 | 10MB 넘는 파일 첨부 → 거부되고 화면이 깨지지 않는지 | |
| 21 | 메모 제목 검색 / [내 메모] [공유 메모] 필터 | |
| 22 | 모바일에서 메모 목록 → 선택 → 보기/수정 흐름 | |
| 23 | 관리자 Storage 탭 : 디스크 / 앱 데이터 / 파일 수 / [새로고침] | |
| 24 | 일반 사용자로 `/admin/storage` 직접 입력 → 403 | |

> **평문 HTTP 로 서비스하면 PWA(서비스워커 / 홈 화면에 추가)는 동작하지 않는다.**
> 브라우저는 서비스워커를 `https:` 또는 `localhost` 에서만 등록한다.
> `http://<호스트명>/` 이나 `http://<호스트명>:19780/` 은 secure context 가 아니다.
>
> - 서비스워커 등록이 실패한다 -> 오프라인 캐시 없음
> - 설치 버튼이 나타나지 않는다 (`beforeinstallprompt` 가 발생하지 않음)
> - 앱은 `register("/sw.js").catch(...)` 로 실패를 무시하므로 **오류 없이 나머지 기능은 전부 정상**이다.
>   모바일 브라우저로 접속해서 쓰는 것도 된다.
>
> 설치형 앱처럼 쓰려면 사내 CA 인증서로 HTTPS 로 올린다. (→ [13. 내부 HTTPS](#13-내부-https-선택))
> `/static/`, `/manifest.webmanifest`, `/sw.js` 자체는 HTTP 에서도 200 으로 내려온다.
> (서비스워커 *등록*만 브라우저가 거부한다)

서버에서 한 번에 훑는 명령:

```bash
curl -fsS http://127.0.0.1/health
curl -fsSI http://127.0.0.1/sw.js | grep -i service-worker-allowed   # : /
curl -fsS -o /dev/null -w '%{http_code}\n' http://127.0.0.1/manifest.webmanifest
curl -fsS -o /dev/null -w '%{http_code}\n' http://127.0.0.1/static/shared.css
docker compose exec app claude -p "Respond only with OK"
```

메모와 Storage 도 서버에서 바로 확인할 수 있다. (`-b` 쿠키는 로그인 후 받은 것)

```bash
# 컨테이너 안에서 : 데이터가 실제 볼륨에 들어가는지
docker compose exec app ls -ld /var/lib/claude-web/notes
docker compose exec app sh -c 'ls -R /var/lib/claude-web/notes | head -20'

# down/up 후에도 메모가 남는지 (36번 요구사항)
docker compose down && docker compose up -d
docker compose exec -T app python -c   "from db import connect; c=connect(); print('notes:', c.execute('SELECT COUNT(*) FROM notes').fetchone()[0],    'attachments:', c.execute('SELECT COUNT(*) FROM note_attachments').fetchone()[0])"
```

---

## 12. 방화벽 / SELinux

### 방화벽 — 80(또는 443)만 연다

```bash
# Rocky/RHEL (firewalld)
firewall-cmd --permanent --add-service=http
# HTTPS 를 쓰면: firewall-cmd --permanent --add-service=https
firewall-cmd --reload
firewall-cmd --list-all

# Ubuntu (ufw)
# ufw allow 80/tcp && ufw enable && ufw status
```

앱 컨테이너의 8080 은 host 에 publish 하지 않는다. 열리는 포트는 nginx 의 80 뿐이다.

```bash
ss -ltnp | grep -E ':80|:8080'    # :80 만 LISTEN. :8080 은 없어야 한다
```

> Docker 는 publish 한 포트를 위해 iptables 규칙을 직접 넣기 때문에, firewalld 의
> zone 설정과 무관하게 이미 열려 있을 수 있다. 위 명령은 그래도 넣어 둔다 (정책 일관성).
> **실제로 열렸는지는 다른 PC 에서 접속해 확인한다.**
> ```bash
> # 클라이언트 PC 에서
> curl -I http://<사내 호스트명>/health
> ```

### SELinux (Rocky/RHEL 기본 enforcing)

`compose.yml` 의 bind mount 에 `:z` 를 붙여 두었다. Docker 가 마운트 대상에
`container_file_t` 라벨을 붙여 주므로 **추가 작업이 필요 없다.** SELinux 를 끄지 말 것.

```bash
getenforce                                   # Enforcing
ls -Zd /var/lib/claude-web /opt/claude-web   # container_file_t 로 바뀌어 있다
```

nginx 도 컨테이너로 돌기 때문에 `httpd_can_network_connect` 같은 SELinux boolean 변경은
필요 없다. (host 의 nginx 를 쓰는 구성에서만 필요하다 → [21. Option 비교](#21-nginx-를-컨테이너로-vs-host-에))

문제가 생기면 먼저 원인을 본다. 끄지 말고 필요한 것만 허용한다.

```bash
ausearch -m AVC -ts recent | tail -20
```

---

## 13. 내부 HTTPS (선택)

사내 도메인은 Let's Encrypt 로 발급받기 어렵다. HTTP-01 은 인터넷에서 도달 가능해야
하고, DNS-01 은 공인 DNS 가 필요하다. **사내 CA 로 발급받은 인증서를 쓴다.**
클라이언트 PC 에 사내 루트 CA 가 신뢰 목록에 들어 있어야 한다.

```bash
install -d -m 0750 /etc/claude-web/certs
install -m 0644 claude-web.crt /etc/claude-web/certs/      # 서버 인증서 + 중간 CA 체인
install -m 0600 claude-web.key /etc/claude-web/certs/      # 개인키
```

1. `docker/nginx-https.conf.example` 을 `docker/nginx.conf` 로 복사하고
   `server_name` 과 인증서 파일명을 고친다.
2. `compose.yml` 의 nginx 서비스에서 `443:443` 포트와 `certs` 볼륨 주석을 푼다.
3. `.env` 에서 `SESSION_COOKIE_SECURE=1` 로 바꾼다.
4. `docker compose up -d`

인증서 자체는 **git 과 이미지에 절대 넣지 않는다.** (`.gitignore` 가 `*.crt`, `*.key`,
`*.pem` 을 이미 막고 있다)

---

## 14. 운영 명령

`cd /opt/claude-web/claude-web` 에서 실행한다.

```bash
docker compose ps                     # 상태
docker compose up -d                  # 기동 / 변경 반영
docker compose down                   # 정지 + 컨테이너 삭제 (데이터/인증은 남는다)
docker compose restart app            # 앱만 재시작 (소스 변경 반영)
docker compose restart nginx          # nginx 설정 변경 반영

docker compose logs -f app            # 앱 + gunicorn 로그
docker compose logs -f nginx          # 액세스/에러 로그
docker compose logs --tail=100 app    # 최근 100줄

docker compose exec app bash          # 컨테이너 셸 (claudeweb 사용자)
docker compose exec app claude --version
docker compose exec app claude -p "Respond only with OK"
docker compose top app                # 컨테이너 안 프로세스 (이미지에 ps 가 없다)

docker stats --no-stream              # CPU/메모리
docker system df                      # 디스크 사용량
```

### 로그

앱과 gunicorn 은 stdout/stderr 로만 출력한다. 컨테이너 안에 로그 파일을 쌓지 않는다.
`compose.yml` 에서 `json-file` 드라이버에 `max-size=10m, max-file=5` 를 걸어 두었으므로
서비스당 최대 50MB 에서 자동 순환된다. 별도 logrotate 설정이 필요 없다.

Claude 오류도 앱 로그에 남는다. 앱이 내보내기 전에 `sk-ant-...`, `Bearer ...`,
`x-api-key ...` 패턴을 `[REDACTED]` 로 바꾸므로 자격증명은 로그에 남지 않는다.

---

## 15. 업데이트

### 15-A. 소스만 바뀐 경우 (대부분)

```bash
cd /opt/claude-web/claude-web
./scripts/docker-backup.sh            # DB/업로드/.env 백업
git -C .. pull --ff-only
docker compose restart app
docker compose logs --tail=100 app
curl -fsS http://127.0.0.1/health
```

또는 한 번에:

```bash
./scripts/docker-update.sh
```

**소스는 bind mount 라서 `git pull` 직후 컨테이너에 곧바로 보인다. 하지만 gunicorn
워커가 파이썬 모듈과 Jinja 템플릿을 메모리에 들고 있으므로 재시작해야 반영된다.**
실측 결과(검증됨):

| 바꾼 것 | 재시작 없이 | `restart app` 후 |
|---|---|---|
| `templates/*.html` | 반영 안 됨 | 반영됨 |
| `*.py` | 반영 안 됨 | 반영됨 |
| `static/*` | 반영됨 (매 요청 파일을 읽는다) | 반영됨 |

`static/` 을 고쳤고 PWA 캐시를 무효화해야 하면 `static/sw.js` 의 `VERSION`
문자열(`chat-bot-v7`)을 올린 뒤 재시작한다.
`shared.css` / `type.css` / `fonts/` 는 서비스워커의 `SHELL` 목록에 있어서,
버전을 올리지 않으면 이미 방문한 브라우저에 예전 화면이 그대로 남는다.

### 15-B. requirements.txt / 런타임이 바뀐 경우

```bash
./scripts/docker-backup.sh
git -C .. pull --ff-only
docker compose build app
docker compose up -d app
docker compose exec app python -m pip list | head -20
```

또는:

```bash
./scripts/docker-update.sh --build
```

**`restart` 만으로는 새 패키지가 반영되지 않는다.** 실측 확인:

```text
requirements.txt 에 패키지 추가 -> docker compose restart app
  -> ModuleNotFoundError: No module named 'tabulate'
docker compose build app && docker compose up -d app
  -> import OK
```

### 15-C. 이미지를 통째로 교체하는 경우

```bash
gunzip -c claude-web-runtime-1.0.1.tar.gz | docker load
sed -i 's/^CLAUDE_WEB_IMAGE=.*/CLAUDE_WEB_IMAGE=claude-web-runtime:1.0.1/' .env
docker compose up -d
docker inspect -f '{{.Config.Image}}' claude-web-app-1
```

되돌릴 때는 `.env` 의 그 한 줄을 이전 태그로 바꾸고 `docker compose up -d`.
DB / 업로드 / Claude 인증은 그대로 유지된다. (검증됨)

### DB 마이그레이션

스키마 변경은 앱이 기동할 때(`app.py` import 시점) 자동으로 수행된다.

- 기존 DB 를 지우지 않는다. 필요한 테이블/컬럼만 추가한다.
- 변경이 필요할 때만 **먼저 자동 백업**을 만든다. (`backups/chat.db.backup-migrate-…`)
- 모든 DDL/DML 을 하나의 트랜잭션에서 실행한다. 중간에 실패하면 전부 롤백되어
  기존 DB 가 그대로 남는다.
- `PRAGMA user_version` 으로 적용 여부를 기록한다. 매번 기동해도 안전하다.

명시적으로 먼저 돌려 보고 싶으면:

```bash
docker compose run --rm --no-deps app python app.py migrate
```

```text
변경할 내용이 없습니다.           # 최신 상태
마이그레이션 완료.  / 백업: ...   # 적용됨
```

#### 스키마 버전 이력

| 버전 | 내용 |
|---|---|
| v2 | 로그인 / 권한 도입 (`users`, `settings`, `sessions.owner_id`, `visibility`) |
| v3 | `attachments.file_path` 를 `UPLOAD_DIR` 기준 상대경로로 변환 |
| v4 | 메모 기능: `notes`, `note_attachments` 테이블 추가 |
| **v5** | **메모 댓글: `note_comments` 테이블 추가** |

v4 와 v5 는 **테이블을 추가만** 한다. 기존 테이블의 컬럼과 데이터를 전혀 건드리지
않아 채팅 기능에 영향이 없다. 실제로 데이터가 들어 있는 v3 DB 로 검증했다.

v5 의 `note_comments` 는 `notes` 를 참조하는 새 테이블 하나뿐이다. 되돌려야 하면
코드를 이전 커밋으로 내리기만 하면 된다. 테이블이 남아 있어도 예전 코드는 그
테이블을 쳐다보지 않는다. (`PRAGMA user_version` 만 5 로 남는데, 이는 다음
기동에서 다시 5 로 맞춰지므로 문제가 되지 않는다)

```bash
# 배포 전에 현재 버전 확인
docker compose exec -T app python -c   "from db import connect; print('user_version =', connect().execute('PRAGMA user_version').fetchone()[0])"

# 적용 후 확인
docker compose exec -T app python -c   "from db import connect; c=connect(); print([r[0] for r in c.execute(   \"SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'note%'\")])"
# -> ['notes', 'note_attachments']
```

---

## 16. 백업과 복구

```bash
./scripts/docker-backup.sh          # 기본 14일 보관
./scripts/docker-backup.sh 30       # 30일 보관
```

모든 작업을 컨테이너 안에서 한다. host 에 `sqlite3` 나 `tar` 를 설치할 필요가 없고,
host 에서 `/var/lib/claude-web` 에 쓸 권한(root)도 필요 없다.

| 대상 | 방법 |
|---|---|
| `chat.db` | `python app.py backup` = sqlite **온라인 백업 API**. WAL 을 쓰므로 단순 `cp` 는 일관성이 보장되지 않는다. 직후 `PRAGMA integrity_check` 로 검증한다 |
| `uploads/` | `uploads-<시각>.tar.gz` |
| `.env` | `env-<시각>.bak` (0600). **SECRET_KEY 가 들어 있다** |
| Claude 인증 | **일부러 제외한다** (아래) |

결과물은 `/var/lib/claude-web/backups/` 에 쌓인다. 이 디렉터리를 host 의 기존 백업
도구로 서버 밖으로 가져간다.

Claude 인증(`claude-web-home` 볼륨)을 백업 대상에서 뺀 이유:
그 볼륨에는 사용자 계정 OAuth 자격증명(`~/.claude/.credentials.json`, 0600)이 들어
있다. 평문 자격증명을 백업 파일로 복사해 두면 보관/유출 위험이 그만큼 커지는데,
잃어버렸을 때 복구는 `docker compose exec app claude` 로 다시 로그인하면 끝난다.
그래도 보관해야 한다면 root 로, 접근이 제한된 곳에만 둔다.

```bash
docker run --rm -v claude-web-home:/h -v "$PWD":/out alpine \
    tar -czf /out/claude-home.tar.gz -C /h .
chmod 600 claude-home.tar.gz
```

### 자동 백업 (cron)

```bash
cat >/etc/cron.d/claude-web-backup <<'EOF'
SHELL=/bin/bash
PATH=/usr/local/sbin:/usr/local/bin:/sbin:/bin:/usr/sbin:/usr/bin
30 3 * * * root cd /opt/claude-web/claude-web && ./scripts/docker-backup.sh 14 >>/var/log/claude-web-backup.log 2>&1
EOF
```

### 복구

```bash
cd /opt/claude-web/claude-web
docker compose stop app

# 백업을 현재 DB 자리로 되돌린다 (컨테이너 안에서)
docker compose run --rm --no-deps app sh -c '
  set -e
  cd /var/lib/claude-web
  cp chat.db chat.db.before-restore 2>/dev/null || true
  rm -f chat.db-wal chat.db-shm
  cp backups/chat.db.backup-manual-20260930-025613 chat.db
  chmod 600 chat.db
'
# 업로드도 되돌릴 때
docker compose run --rm --no-deps app sh -c '
  tar -xzf /var/lib/claude-web/backups/uploads-20260930-115610.tar.gz \
      -C /var/lib/claude-web
'
# 메모 첨부도 되돌릴 때
docker compose run --rm --no-deps app sh -c '
  tar -xzf /var/lib/claude-web/backups/notes-20260930-115610.tar.gz \
      -C /var/lib/claude-web
'
docker compose start app
docker compose logs --tail=50 app
```

복구한 DB 가 오래된 스키마여도 기동할 때 자동으로 마이그레이션된다.
`.env` 를 복구했다면 **SECRET_KEY 가 그때 값과 같아야** 저장해 둔 Claude API Key 가
복호화된다.

---

## 17. 재부팅

별도 systemd unit 을 만들지 않는다. 다음 두 가지로 충분하다.

```bash
systemctl is-enabled docker          # enabled
grep -n 'restart:' compose.yml       # unless-stopped (app, nginx)
```

서버가 재부팅되면 Docker daemon 이 올라오고, `unless-stopped` 정책에 따라
`app` 과 `nginx` 가 자동으로 다시 시작된다. `init` 은 1회성이라 재실행되지 않지만
데이터 디렉터리는 이미 만들어져 있어 문제가 없다.

프로세스가 죽었을 때의 자동 복구도 같은 정책이 처리한다. (검증됨: gunicorn master 를
강제 종료 → 컨테이너 종료 → 자동 재시작 `RestartCount=1` → healthy 복귀)

> `docker stop` / `docker kill` 로 **사람이 직접 멈춘** 컨테이너는 정책상 자동으로
> 다시 시작하지 않는다. `docker compose up -d` 로 다시 올린다.

재부팅 후 확인:

```bash
reboot
# 재접속 후
docker compose ps                  # app / nginx 가 Up (healthy)
curl -fsS http://127.0.0.1/health
docker compose exec app claude -p "Respond only with OK"
```

---

## 18. 볼륨 / 권한 구조

| 컨테이너 경로 | host | 모드 | 소유자 | 내용 |
|---|---|---|---|---|
| `/app` | `/opt/claude-web/claude-web` | `ro,z` | (host 그대로) | 소스. 앱이 쓰지 않는다 |
| `/var/lib/claude-web` | `/var/lib/claude-web` | `rw,z` | `claudeweb:claudeweb 0750` | chat.db, uploads, **notes**, workspace, backups |
| `/home/claudeweb` | volume `claude-web-home` | `rw` | `claudeweb:claudeweb 0700` | Claude 인증, 세션 히스토리 |
| `/etc/nginx/conf.d/default.conf` | `./docker/nginx.conf` | `ro,z` | (host 그대로) | nginx 설정 |

```bash
docker compose exec app ls -ld /var/lib/claude-web /var/lib/claude-web/uploads
docker compose exec app ls -l  /var/lib/claude-web/chat.db          # -rw------- claudeweb
docker compose exec app ls -ld /home/claudeweb/.claude              # drwx------ claudeweb
docker compose exec app touch /app/x                                # Read-only file system (정상)
docker volume ls | grep claude-web
```

몇 가지 중요한 점:

- **SQLite WAL 은 DB 파일이 있는 디렉터리에도 쓰기 권한이 필요하다.**
  `chat.db-wal`, `chat.db-shm` 을 같은 디렉터리에 만든다. 파일 권한만 맞추고
  디렉터리를 읽기 전용으로 두면 `attempt to write a readonly database` 가 난다.
- `chat.db` 는 0600 이다. 앱이 기동할 때마다 스스로 조정한다. 설정값(암호화된
  API Key 포함)이 들어 있기 때문이다.
- `/app` 이 읽기 전용이어도 문제가 없다. 앱은 `.pyc` 를 만들지 않고
  (`PYTHONDONTWRITEBYTECODE=1`), bootstrap 토큰도 `/var/lib/claude-web` 에 쓴다.
- 컨테이너는 `/app` 과 `/var/lib/claude-web` 만 본다. host 의 다른 디렉터리,
  `/var/run/docker.sock`, `privileged` 는 쓰지 않는다. Claude 가 접근할 수 있는
  범위도 이 두 곳이다.
- Claude 에 넘기는 `--add-dir` 인자는 앱이 `UPLOAD_DIR`(= `/var/lib/claude-web/uploads`)
  하나만 준다. 인자 순서(`-p` 직후 `--add-dir`, 그 뒤 `--output-format json`)는
  현재 CLI 기준으로 동작하는 형태이므로 건드리지 않는다.
- 첨부파일 경로는 DB 에 `UPLOAD_DIR` 기준 **상대경로**로 저장된다
  (`project_1/session_2/<uuid>.png`). 그래서 개발 PC 에서 만든 DB 를 그대로 서버로
  옮겨도 첨부가 깨지지 않는다. 절대경로가 남아 있는 옛 DB 는 마이그레이션 v3 가
  자동으로 변환한다.
- 메모 첨부도 같은 규칙이다. `NOTES_DIR`(= `/var/lib/claude-web/notes`) 기준
  상대경로(`user_3/note_12/<uuid>.pdf`)로 저장된다. 같은 데이터 볼륨 안이라
  **compose 설정을 바꿀 필요가 없고**, `docker compose down` → `up -d` 후에도
  메모와 첨부가 그대로 남는다.

---

## 19. 개발 PC 데이터를 옮겨오는 경우

```bash
# 개발 PC 에서
#   claude-web/data/chat.db      -> 서버 /var/lib/claude-web/chat.db
#   claude-web/data/uploads/     -> 서버 /var/lib/claude-web/uploads/
# (scp / sftp 등으로 옮긴다. chat.db-wal, chat.db-shm 은 옮기지 않는다)

cd /opt/claude-web/claude-web
docker compose stop app
docker compose run --rm --no-deps app sh -c 'chown -R 1000:1000 /var/lib/claude-web' || true
docker compose start app
docker compose logs --tail=60 app     # [migrate] 줄을 확인
```

`init` 컨테이너가 소유권을 정리하므로 `docker compose up -d` 만으로도 된다.
기동 로그에 다음이 보이면 첨부 경로가 자동 변환된 것이다.

```text
[migrate] 백업: /var/lib/claude-web/backups/chat.db.backup-migrate-...
[migrate] attachments.file_path 12건을 상대경로로 변환 (기준: /var/lib/claude-web/uploads)
```

옮긴 뒤 관리자 페이지에서 **Claude CLI 경로를 컨테이너 기준으로 고친다.**
개발 PC 값(`C:/...`)이 DB 에 남아 있으면 Claude 호출이 전부 실패한다.

```text
Claude CLI 경로   -> /usr/bin/claude
Working Directory -> /var/lib/claude-web/workspace
```

---

## 20. 문제 해결

### 웹에 접속이 안 된다

```bash
docker compose ps                        # 둘 다 Up 인가
curl -fsS http://127.0.0.1/healthz       # nginx 자체가 사는가
curl -fsS http://127.0.0.1/health        # app 까지 가는가
ss -ltnp | grep :80
firewall-cmd --list-all                  # http 서비스가 있는가
getent hosts <사내 호스트명>              # 사내 DNS 가 조회되는가
```

### 502 Bad Gateway

nginx 는 살아 있고 app 에 못 붙는 상태다.

```bash
docker compose logs --tail=80 app        # 앱이 기동 중 죽지 않았는지
docker compose logs --tail=40 nginx      # connect() failed / no live upstreams
docker compose exec nginx wget -q -O - http://app:8080/health
```

흔한 원인:

| 증상 | 원인 | 조치 |
|---|---|---|
| 앱 로그에 traceback | 소스 오류 / `.env` 값 오류 | 로그의 예외를 고친다 |
| 앱 로그가 비어 있고 `Exited` | `.env` 문법 오류, 마이그레이션 실패 | `docker compose logs app` 전체 확인 |
| 앱은 healthy 인데 502 | nginx 가 옛 IP 를 보고 있음 | 현재 설정은 `resolver 127.0.0.11` 로 매 요청 재해석한다. `docker/nginx.conf` 를 임의로 `upstream` 블록으로 바꾸면 이 문제가 생긴다 |
| 응답이 오래 걸리다 502/504 | 타임아웃 사슬 역전 | Claude 180 < gunicorn 300 < nginx 360 을 확인 |

### Claude 호출이 실패한다

```bash
docker compose exec app claude -p "Respond only with OK"
```

| 메시지 | 원인 | 조치 |
|---|---|---|
| `Not logged in · Please run /login` | 인증 없음/만료 | `docker compose exec app claude` 로 재로그인 |
| `Claude CLI 를 찾을 수 없습니다` | 관리자 설정의 CLI 경로 오류 | `/usr/bin/claude` 로 수정 |
| `Working Directory 경로가 올바르지 않습니다` | workdir 없음 | `/var/lib/claude-web/workspace` |
| `시간 초과(180초)` | 질문이 무겁다 | 관리자 페이지 timeout ↑ + gunicorn/nginx 도 함께 ↑ |
| 응답이 느리고 409 가 섞인다 | 같은 세션 동시 요청 | 정상 동작. 세션 lock 이다 |
| `permission denied` 계열 | 자격증명 파일 소유자 문제 | `ls -l /home/claudeweb/.claude/` 확인, root 소유면 `docker compose restart app` (init 이 정리한다) |

인증정보가 root 소유로 만들어지는 사고는 `docker exec -u root` 로 `claude` 를
실행했을 때 일어난다. 항상 기본 사용자(`claudeweb`)로 실행한다.

### app 컨테이너가 `Restarting (3)` 으로 계속 재시작한다

gunicorn 이 종료 코드 3 으로 죽는 것은 **앱 모듈 import 실패**다. 로그에 파이썬
traceback 이 그대로 남는다.

```bash
docker compose logs app --tail=60
```

| traceback 마지막 줄 | 원인 | 조치 |
|---|---|---|
| `PermissionError: ... '/app/.env'` | `.env` 가 root 전용(0600)이라 uid 1000 이 못 읽음 | 최신 소스는 이 경우 환경변수만 쓰고 계속 기동한다. 구버전이면 `git pull` 하거나 `chown root:1000 .env && chmod 640 .env` |
| `PermissionError: ... '/var/lib/claude-web...'` | 데이터 디렉터리 소유자 불일치 | `docker compose up -d` (init 이 chown 한다) |
| `ModuleNotFoundError` | `requirements.txt` 변경 후 재빌드 안 함 | `docker compose build app && docker compose up -d app` |
| `FileNotFoundError: /app/app.py` | 소스 마운트 누락 | `compose.yml` 의 `./:/app:ro,z` 확인, 실행 디렉터리 확인 |
| `sqlite3.DatabaseError` | DB 손상 | [16. 복구](#16-백업과-복구) |

### DB 오류

```bash
docker compose exec app ls -l /var/lib/claude-web/
docker compose exec app python -c \
  "import sqlite3;print(sqlite3.connect('/var/lib/claude-web/chat.db').execute('PRAGMA integrity_check').fetchone())"
```

| 메시지 | 원인 | 조치 |
|---|---|---|
| `attempt to write a readonly database` | DB **디렉터리** 쓰기 권한 없음 (WAL) | `docker compose up -d` (init 이 chown) |
| `database is locked` | 장시간 트랜잭션 | `busy_timeout` 15초가 이미 걸려 있다. 반복되면 로그 확인 |
| `unable to open database file` | 볼륨 미마운트 | 앱 로그의 `볼륨으로 마운트되어 있지 않습니다` 경고 확인 |

### 이미지 업로드가 실패한다

| 증상 | 원인 | 조치 |
|---|---|---|
| nginx 413 페이지 | `client_max_body_size` < 요청 크기 | `docker/nginx.conf` 수정 후 `restart nginx` |
| 앱의 한글 "용량 초과" 메시지 | `MAX_UPLOAD_MB` 초과 | 정상 동작 |
| `허용되지 않는 형식` | png/jpg/jpeg/webp/gif 외 | 확장자와 실제 매직바이트를 둘 다 검사한다 |
| 업로드는 되는데 Claude 가 못 읽는다 | `--add-dir` 경로 밖 | `UPLOAD_DIR` 이 `/var/lib/claude-web/uploads` 인지 확인 |

### 디스크

```bash
df -h /var /opt
du -sh /var/lib/claude-web/*
docker system df
docker image prune -f              # 태그 없는 옛 이미지 정리
docker builder prune -f            # 빌드 캐시 정리
```

`docker system prune -a` 는 쓰지 말 것. 되돌릴 이전 태그 이미지까지 지운다.

---

## 21. nginx 를 컨테이너로 vs host 에

| | Option A — nginx 도 컨테이너 (현재 구성) | Option B — host 의 nginx |
|---|---|---|
| 설치 | `docker compose up -d` 하나로 끝 | host 에 nginx 설치/설정/SELinux 작업 추가 |
| 재현성 | 설정이 git 에 있고 이미지 태그로 고정된다 | host 설정이 서버마다 갈린다 |
| 컨테이너 IP 변경 | `resolver` 로 자동 추적 | host → `127.0.0.1:8080` 이라 영향 없음 |
| SELinux | 추가 작업 없음 | `setsebool -P httpd_can_network_connect 1` 필요 |
| 한 서버에 다른 사이트도 있을 때 | 80 포트 충돌 | 기존 nginx 에 server 블록만 추가하면 된다 |
| 사내 표준 인증서 배포 자동화 | 볼륨으로 마운트해야 한다 | 기존 체계 그대로 |

**권장: Option A.** 이 서버가 claude-web 전용이고 사내 HTTP(또는 사내 CA 인증서)로
서비스하므로, 설치가 한 번에 끝나고 설정이 git 에 남는 쪽이 낫다.

이미 host 에 nginx 가 돌고 있어 80 포트를 쓸 수 없다면 Option B 로 간다.

```bash
# compose.yml 에서 nginx 서비스를 지우고, app 을 루프백에만 publish 한다
#   app:
#     ports:
#       - "127.0.0.1:8080:8080"
# 그 다음 host nginx 설정은 deploy/nginx-http.conf.example 을 쓴다.
setsebool -P httpd_can_network_connect 1        # Rocky/RHEL 에서 필수
```

---

## 22. 실제로 검증한 것 / 검증하지 못한 것

정직하게 구분한다.

### 검증함 (개발 PC 의 Docker Engine 29.5.2 / Compose v5.1.4 에서 실제 실행)

| 항목 | 결과 |
|---|---|
| 이미지 빌드 (`docker compose build`) | 성공. Python 3.12.14 / Debian 13 |
| Claude CLI 설치 경로·버전 | `/usr/bin/claude`, `2.1.280 (Claude Code)` |
| Claude 서명키 지문 검증 | 빌드 단계에서 통과 |
| pip 패키지 | Flask 3.1.3 / Werkzeug 3.1.9 / gunicorn 26.2.0 / cryptography 50.0.1 / python-dotenv 1.2.3 |
| `docker compose up -d` | init → app → nginx, 둘 다 `healthy` |
| gunicorn 워커 수 | `docker compose top app` 으로 master + worker 1개 확인 |
| `docker compose exec` 기본 사용자 | `uid=1000(claudeweb)` (root 아님) |
| `/app` 읽기 전용 | `touch /app/x` → `Read-only file system` |
| 데이터 권한 | `/var/lib/claude-web` 0750 claudeweb, `chat.db` 0600 |
| 스키마 | `user_version=4`, `journal_mode=wal` |
| 기능 테스트 27항목 (nginx 경유) | 전부 통과 — 로그인, 한글 프로젝트/세션 생성, private/public, 이미지 업로드, 첨부 다운로드 md5 일치, `/sw.js`+`Service-Worker-Allowed: /`, manifest, static, `/admin/`, 설정 API, API Key 비노출, 비로그인 private 세션 401 |
| 최초 관리자 생성 | `python app.py create-admin` 로 생성 |
| `down` → `up` 데이터 보존 | users/projects/sessions/messages/attachments/settings 전부 동일, 업로드 파일 유지 |
| `down` → `up` Claude 인증 볼륨 보존 | `~/.claude` 내용 유지 (표식 파일 + `projects/`, `sessions/`) |
| 이미지 태그 교체 (1.0.0 → 1.0.1) | 데이터·인증 보존, 새 태그로 기동 |
| 소스만 수정 + `restart app` | 템플릿·파이썬 변경 모두 반영. **이미지 재빌드 없음** |
| 소스 수정 후 재시작 안 함 | 반영되지 않음 (문서대로) |
| requirements 변경 + `restart` 만 | `ModuleNotFoundError` (문서대로) |
| requirements 변경 + `build` + `up -d` | 반영됨 |
| app 컨테이너 IP 변경 후 nginx | IP `172.22.0.2 → 172.22.0.7`, nginx 재시작 없이 HTTP 200 |
| `restart: unless-stopped` | gunicorn master 강제 종료 → 컨테이너 자동 재시작, healthy 복귀 |
| 명시적 마이그레이션 | `docker compose run --rm --no-deps app python app.py migrate` 동작 |
| 백업 스크립트 | DB 온라인 백업 + `integrity_check ok` + uploads/notes tar + `.env` 0600 + 보관기간 정리 |
| **v3 → v4 마이그레이션** | 데이터가 든 v3 DB(users 2 / sessions 2 / messages 5 / attachments 1)로 실행 → 전부 보존, 자동 백업 생성, `integrity_check ok`, 재실행 무해 |
| **메모 + Storage 기능 116항목** | 전부 통과 — 생성/조회/수정/삭제, private·public 권한, 첨부 업로드·다운로드·삭제, 10MB 제한, 위장 파일 거부, CSRF, 관리자 전용 Storage, 기존 기능 회귀 |
| 같은 116항목을 Linux 컨테이너에서 재실행 | 전부 통과 (uid 1000, 데이터는 `/var/lib/claude-web`) |
| 저장공간 계산 정확도 | `storage.py` 결과 = `os.walk` 합계와 완전 일치. 데이터 루트 안의 항목 중복 집계 없음 (합계 = 루트 전체와 일치) |
| 심볼릭 링크 무시 | Linux 에서 디렉터리 링크(`/usr`)·파일 링크·순환 링크 모두 따라가지 않음. 권한 없는 디렉터리를 만나도 예외 없음 |
| 렌더된 JS 문법 | `/`, `/notes`, `/admin/storage` 세 화면의 인라인 JS 를 `node --check` 로 검사 통과. `innerHTML`/`eval` 미사용 |

### 검증하지 못함 (운영 서버에서 확인해야 한다)

| 항목 | 이유 |
|---|---|
| **`claude -p` 실제 응답** | 컨테이너에 인증정보가 없다. `Not logged in · Please run /login` 까지만 확인. **§8 을 끝낸 뒤 반드시 직접 확인한다** |
| **메모/Storage 화면의 실제 브라우저 조작** | 개발 PC 의 Chrome 이 테스트 서버에 접근하지 못하는 환경이었다. HTTP 계층은 116항목으로 검증했고 JS 는 문법 검사까지 했지만, **클릭·모바일 레이아웃은 §11 의 14~24번으로 직접 확인해야 한다** |
| Claude 최초 로그인 흐름 | 브라우저 + 사내 Claude 계정이 필요하다 |
| Claude `--resume` 문맥 유지 / 이미지 분석 | 인증 후에만 가능하다 |
| Rocky Linux 에서의 Docker 설치 | 개발 PC 는 Windows 다. 공식 문서 명령을 그대로 실었다 |
| SELinux `:z` 라벨 동작 | SELinux 가 없는 환경이라 무시되었다. Rocky 에서 `ls -Zd` 로 확인할 것 |
| firewalld 규칙 | 같은 이유 |
| host bind mount 권한 (`/var/lib/claude-web`) | Windows 에서는 chown 의미가 달라 named volume 으로 시험했다. 리눅스에서 `ls -ld` 로 확인할 것 |

> **이 항목에서 실제로 문제가 나왔고 고쳤다.** Rocky 9 운영 서버에서
> `.env` 가 `root:root 0600` 이라 uid 1000 인 컨테이너가 읽지 못해
> `PermissionError: '/app/.env'` 로 앱이 기동하지 못했다. Windows 볼륨은 파일
> 권한을 무시하므로 개발 PC 테스트에서는 드러나지 않았다.
> `config.py` 가 이 경우 환경변수만 쓰고 계속 기동하도록 고쳤다. (compose 의
> `env_file` 이 이미 같은 값을 넣어준다)
| 실제 재부팅 | 정책 설정과 프로세스 종료 시 자동 복구까지만 확인했다 |
| 사내 DNS / 다른 PC 에서의 접속 | 사내망이 필요하다 |
| 사내 CA HTTPS | 인증서가 필요하다 |

---

## 부록. 새 서버 설치 체크리스트

```text
[ ]  1. OS 확인               cat /etc/os-release
[ ]  2. Docker 설치           dnf/apt 로 docker-ce + compose plugin
[ ]  3. Docker 자동 시작      systemctl enable --now docker
[ ]  4. 버전 확인             docker --version / docker compose version
[ ]  5. git 설치              dnf -y install git
[ ]  6. 소스 clone            git clone ... /opt/claude-web
[ ]  7. 작업 디렉터리 이동     cd /opt/claude-web/claude-web
[ ]  8. 데이터 디렉터리        install -d -m 0750 /var/lib/claude-web
[ ]  9. .env 작성             cp .env.example .env && chmod 600 .env
[ ] 10. SECRET_KEY 생성/기입   python3 -c "import secrets;print(secrets.token_hex(32))"
[ ] 11. 이미지 준비            docker compose build   (또는 docker load)
[ ] 12. 기동                  docker compose up -d
[ ] 13. 상태 확인             docker compose ps / logs app / curl /health
[ ] 14. Claude 최초 인증       docker compose exec app claude
[ ] 15. Claude 인증 확인       docker compose exec app claude -p "Respond only with OK"
[ ] 16. 인증 영속성 확인       down -> up -> 다시 위 명령
[ ] 17. 최초 관리자            docker compose exec app python app.py create-admin
[ ] 18. 방화벽                firewall-cmd --permanent --add-service=http && --reload
[ ] 19. 브라우저 로그인        http://<사내 호스트명>/
[ ] 20. 관리자 Claude 설정      CLI 경로 /usr/bin/claude, workdir, timeout, 동시 실행
[ ] 21. 연결 테스트            관리자 > Claude 설정 > Connection Test -> ok/OK
[ ] 22. 사용자 계정 생성
[ ] 23. 기능 테스트 13항목      §11 표
[ ] 24. 백업 1회 수동 실행     ./scripts/docker-backup.sh
[ ] 25. 백업 cron 등록
[ ] 26. 재부팅 시험            reboot -> docker compose ps -> /health -> claude -p
```

---

## 참고

- 컨테이너 없이 OS 에 직접 설치하는 절차: [DEPLOYMENT.md](DEPLOYMENT.md)
- 애플리케이션 구조와 개발 실행: [README.md](README.md)
- 파일 위치
  - `docker/Dockerfile` — 실행환경 이미지
  - `docker/entrypoint.sh` — 데이터 디렉터리/권한 정리 + 권한 강하
  - `docker/nginx.conf` — 리버스 프록시 (HTTP)
  - `docker/nginx-https.conf.example` — 사내 HTTPS 예시
  - `compose.yml` — 운영 구성
  - `compose.dev.yml` — 개발용 override (gunicorn `--reload`). 운영에서 쓰지 않는다
  - `deploy/gunicorn.conf.py` — 워커/타임아웃 (마운트되므로 재빌드 불필요)
  - `scripts/docker-backup.sh`, `scripts/docker-update.sh`
