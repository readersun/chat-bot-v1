# 운영 서버 (nfs-181) 재설치 런북

이 문서는 **이 서버 한 대를 0 부터 다시 만드는 순서**다.
Docker 가 설치되어 있지 않은 상태를 전제로 하고, 위에서 아래로 그대로 실행하면
동작하는 서비스가 된다.

일반적인 Docker 배포 설명(왜 이런 구조인지, 옵션 A/B 비교, 볼륨 설계 등)은
[`DEPLOYMENT_DOCKER.md`](DEPLOYMENT_DOCKER.md) 에 있다.
이 문서는 **그 가이드를 이 서버에 적용하면서 실제로 부딪힌 것들**까지 포함한
서버 고유의 기록이다. 두 문서가 다르면 **이 문서가 이 서버의 정답이다.**

> **주의.** 이 서버는 **Docker** 배포다. `DEPLOYMENT.md` 의
> `/opt/claude-web`, `claudeweb` 계정, `venv/bin/pip`, `systemctl restart
> claude-web` 은 **이 서버에 없다.** 그쪽은 venv + systemd 로 올리는 다른
> 방식의 문서다. 이 서버에서는 아래 [18절](#18-평소-운영) 의 명령을 쓴다.

작성 기준: 2026-09-30 구축, 2026-10-06 SSH 중계 반영

---

## 0. 서버 정보

| 항목 | 값 |
|---|---|
| 통칭 | nfs-181 |
| OS | Rocky Linux 9.7 |
| 작업 계정 | root |
| hostname | `localhost` (변경하지 않음) |
| SELinux | **Disabled** (`getenforce`) |
| 디스크 | LVM VG `rl`, VSize 63G, **VFree 0** |
| 소스 위치 | `/data/chat-bot-v1` (repo 루트) |
| compose 실행 위치 | `/data/chat-bot-v1/claude-web` |
| 데이터 위치 | `/home/claude-web-data` (→ [7절](#7-데이터-디렉터리)) |
| 외부 포트 | **19780** (80 은 다른 웹서버가 사용 중) |
| 접속 주소 | `http://<서버IP>:19780/` |
| 실행환경 이미지 | `claude-web-runtime:1.0.0` |
| Claude CLI | apt 저장소 설치, `/usr/bin/claude` (구축 시 2.1.280) |

### 디스크 레이아웃

```
VG rl   VSize <63.00g   VFree 0        <- 확장 불가. 디스크 추가 없이는 늘릴 수 없다
  rl-root  <39.68g   /  +  /data  +  /var/lib/docker     (49% 사용)
  rl-home   19.37g   /home                               (2% 사용)
  rl-swap   <3.95g
```

`/data` 에는 이 프로젝트 외에 `se-hub`, `se-hub-dev`, `chat-bot-back`, `weak` 가 있다.
즉 **`/` 가 차면 이 서비스만 죽는 게 아니라 서버 전체와 다른 시스템까지 멈춘다.**
그래서 데이터는 `/` 가 아니라 거의 비어 있는 `/home` 에 둔다.

---

## 1. 이 서버에서만 주의할 것 (먼저 읽을 것)

구축하면서 실제로 막혔던 지점이다. 재설치할 때 같은 곳에서 또 막힌다.

| # | 함정 | 결과 | 대응 |
|---|---|---|---|
| 1 | **podman 이 이미 설치되어 있고 다른 시스템이 쓰고 있다** | `docker` 명령이 실제로는 podman. 빌드 로그가 두 번 나오고 compose 동작이 다르다 | `podman-docker` **만** 제거. podman 본체는 손대지 않는다 → [3절](#3-podman-정리-이-서버의-가장-중요한-단계) |
| 2 | `podman-docker` 가 심어둔 `DOCKER_HOST` | 패키지를 지운 뒤에도 그 셸에 값이 남아 `unix:///run/podman/podman.sock` 접속 실패 | `unset DOCKER_HOST` + 새 로그인 셸 → [5절](#5-docker_host-잔존-값-정리) |
| 3 | host 의 **80 포트가 사용 중** | nginx 컨테이너가 `bind: address already in use` 로 기동 실패 | `HTTP_PORT=19780` → [8절](#8-env-작성) |
| 4 | `.env` 가 `root:root 0600` | 컨테이너는 uid 1000 으로 돌아 `.env` 를 못 읽음. 앱이 `Restarting` 무한 반복 | `chown root:1000 .env && chmod 640 .env` → [8절](#8-env-작성) |
| 5 | **VFree 0** | `/` 가 차면 서버 전체가 멈춘다 | 데이터를 `/home` 에 둔다 → [7절](#7-데이터-디렉터리) |
| 6 | 평문 HTTP + 비표준 포트 | PWA(서비스워커)는 동작하지 않는다. 앱 기능은 정상 | 정상 동작이다. HTTPS 를 붙이면 해결 → [15절](#15-동작-확인) |

`DEPLOYMENT_DOCKER.md` 2절에는 Docker 공식 문서대로 `dnf remove ... podman runc` 가
적혀 있다. **이 서버에서는 그 명령을 그대로 실행하면 안 된다.** 3절을 따른다.

---

## 2. 사전 확인

```bash
cat /etc/rocky-release          # Rocky Linux release 9.7
getenforce                      # Disabled
id                              # uid=0(root)
df -h / /home
free -h
```

이미 돌고 있는 컨테이너 도구와 다른 시스템의 컨테이너를 먼저 파악한다.

```bash
rpm -qa | grep -Ei 'podman|docker'
podman ps -a
ss -ltnp | grep -E ':(80|443|19780) '
```

구축 당시 결과 (참고):

```
podman-5.8.2 / podman-compose-1.5.0 / podman-docker-5.8.2 / cockpit-podman
arcturus 컨테이너 2개  Exited (6주 전)  0.0.0.0:2943->80/tcp
:80 은 다른 웹서버가 사용 중
```

> **다른 시스템의 컨테이너(arcturus)와 podman 은 건드리지 않는다.**
> 이 작업 때문에 그 시스템이 멈추면 안 된다. 지우는 것은 `podman-docker` 하나뿐이다.

---

## 3. podman 정리 (이 서버의 가장 중요한 단계)

`podman-docker` 는 `docker` 라는 이름의 래퍼를 설치해 **podman 을 docker 인 척**
실행시키는 패키지다. 이게 깔려 있으면 Docker CE 를 설치해도 `docker` 명령이
podman 으로 갈 수 있다.

```bash
# 지금 docker 가 무엇인지 확인
docker --version                 # "Emulate Docker CLI using podman" 이 보이면 podman 이다
rpm -qf "$(command -v docker)"   # podman-docker-... 이면 래퍼다
```

제거 대상은 이것 하나다. **담당자 확인을 받고 진행한다.**

```bash
dnf remove -y podman-docker
```

제거 후에도 남아 있어야 하는 것 (반드시 확인):

```bash
rpm -qa | grep -Ei 'podman'      # podman, podman-compose, cockpit-podman 은 남아야 한다
podman ps -a                     # arcturus 컨테이너 2개가 그대로 있어야 한다
```

---

## 4. Docker CE 설치

```bash
dnf -y install dnf-plugins-core
dnf config-manager --add-repo https://download.docker.com/linux/centos/docker-ce.repo
dnf -y install docker-ce docker-ce-cli containerd.io \
               docker-buildx-plugin docker-compose-plugin
```

- Rocky 는 RHEL/CentOS 호환 재빌드라 위 `centos` 저장소를 쓴다.
  안 되면 `https://download.docker.com/linux/rhel/docker-ce.repo` 로 바꾼다.
- GPG 키 지문을 물으면 `060A 61C5 1B55 8A7F 742B 77AA C52F EB6B 621E 9F35` 인지 확인한다.
- `dnf remove ... podman` 은 **하지 않는다.** ([3절](#3-podman-정리-이-서버의-가장-중요한-단계))

```bash
systemctl enable --now docker
systemctl is-enabled docker        # enabled  <- 재부팅 후 자동 시작의 전제
docker --version                   # Docker version 29.x.x  (구축 시 29.8.1)
docker compose version             # v2.x 또는 v5.x
```

`systemctl enable docker` + compose 의 `restart: unless-stopped` 조합으로
**재부팅 후 자동 복구된다. 별도의 systemd unit 은 만들지 않는다.**
(podman 은 compose 의 restart 정책만으로는 재부팅 복구가 안 되지만, Docker 는 된다)

---

## 5. `DOCKER_HOST` 잔존 값 정리

`podman-docker` 는 `/etc/profile.d/podman-docker.sh` 로 `DOCKER_HOST` 를 심는다.
패키지를 지우면 파일도 사라지지만 **이미 열려 있는 셸에는 값이 남아 있다.**
이 상태로 빌드하면 이렇게 실패한다.

```
failed to connect to the docker API at unix:///run/podman/podman.sock
```

```bash
echo "[${DOCKER_HOST}]"              # 비어 있어야 정상
unset DOCKER_HOST
docker context use default
ls /etc/profile.d/podman-docker.sh   # No such file  <- 지워졌는지 확인

# 확실하게 하려면 로그아웃 후 다시 접속한다. 새 로그인 셸은 깨끗하다.
docker info --format '{{.ServerVersion}} {{.Name}}'
```

---

## 6. git clone

```bash
dnf -y install git
mkdir -p /data
git clone https://github.com/readersun/chat-bot-v1.git /data/chat-bot-v1
cd /data/chat-bot-v1/claude-web
pwd                               # /data/chat-bot-v1/claude-web
ls compose.yml docker/Dockerfile  # 둘 다 보여야 한다
```

> **compose 명령은 항상 `/data/chat-bot-v1/claude-web` 에서 실행한다.**
> repo 루트(`/data/chat-bot-v1`)에는 `compose.yml` 이 없다.
> 소스는 이미지에 굽지 않고 이 디렉터리를 `/app` 으로 읽기전용 마운트한다.
> 그래서 **`git pull` 만 하면 소스가 컨테이너에 즉시 보인다.**

---

## 7. 데이터 디렉터리

DB / 업로드 이미지 / 메모 첨부파일 / Claude 작업 디렉터리 / 백업이 들어간다.
컨테이너 안 경로는 항상 `/var/lib/claude-web` 이고, **host 경로만 바꿀 수 있다.**

이 서버는 `VFree 0` 이라 `/` 를 늘릴 수 없고, `/` 에는 다른 시스템도 얹혀 있다.
그래서 거의 비어 있는 **`/home` 에 둔다.**

```bash
mkdir -p /home/claude-web-data
chown 1000:1000 /home/claude-web-data
chmod 0750 /home/claude-web-data
ls -ld /home/claude-web-data
```

- `1000:1000` 은 컨테이너 안 `claudeweb` 사용자의 UID/GID 다.
  이 서버에서는 host 의 `gemiso` 계정과 같은 번호라 `gemiso gemiso` 로 표시된다. 정상이다.
- 하위 디렉터리(`uploads`, `notes`, `workspace`, `backups`)는 `init` 컨테이너가
  만들고
  소유권도 맞춰준다. 직접 만들지 않아도 된다.

> `/home` 이 XFS 라 줄여서 `/` 에 붙이는 것은 불가능하다(XFS 는 축소 불가).
> 장기적으로는 VM 에 디스크를 추가해 `vgextend` 하는 것이 정석이다.
> ```bash
> pvcreate /dev/sdb && vgextend rl /dev/sdb
> lvextend -L +50G /dev/mapper/rl-home && xfs_growfs /home
> ```

---

## 8. `.env` 작성

```bash
cd /data/chat-bot-v1/claude-web
cp .env.example .env

# 세션 쿠키 서명 키 생성 (한 번 정하면 바꾸지 않는다)
python3 -c "import secrets;print(secrets.token_hex(32))"
```

`.env` 에서 **반드시 이 서버 값으로 맞출 항목**은 다음 5개다.

```ini
CLAUDE_WEB_IMAGE=claude-web-runtime:1.0.0
HTTP_PORT=19780
HOST_DATA_DIR=/home/claude-web-data
SECRET_KEY=<위에서 생성한 64자 hex>
CLAUDE_BIN=/usr/bin/claude
```

나머지는 `.env.example` 기본값 그대로 두면 된다. 최종적으로 이 값이어야 한다.

```ini
DATABASE_PATH=/var/lib/claude-web/chat.db
UPLOAD_DIR=/var/lib/claude-web/uploads
SESSION_COOKIE_SECURE=0
TRUST_PROXY=1
CLAUDE_PROVIDER=cli
CLAUDE_WORKDIR=/var/lib/claude-web/workspace
CLAUDE_TIMEOUT=180
MAX_CONCURRENT_CLAUDE=3
```

주의할 점:

- `DATABASE_PATH` / `UPLOAD_DIR` 은 **컨테이너 안 경로**다.
  `HOST_DATA_DIR` 을 `/home/claude-web-data` 로 바꿔도 **이 값은 그대로 둔다.**
- `SESSION_COOKIE_SECURE=0` — 평문 HTTP 다. 1 로 두면 브라우저가 쿠키를 저장하지
  않아 로그인 자체가 안 된다.
- `TRUST_PROXY=1` — nginx 컨테이너가 앞에 있다.
- `HOST` / `PORT` 는 Docker 배포에서는 쓰이지 않는다. gunicorn 의 bind 는
  compose 가 `GUNICORN_BIND=0.0.0.0:8080` 으로 넣는다.
- **Compose 는 값 뒤의 `#` 을 주석으로 보지 않는다.** 값 줄에 주석을 쓰지 말 것.
- `SECRET_KEY` 를 나중에 바꾸면 전원 로그아웃되고, 관리자 페이지에 저장한
  Claude API Key 도 이 값에서 파생한 키로 암호화돼 있어 복호화되지 않는다.

### `.env` 권한 — 이 서버에서 실제로 앱이 죽었던 원인

컨테이너는 uid 1000 으로 돌기 때문에 `.env` 가 `root:root 0600` 이면
앱이 파일을 읽지 못한다. 반드시 그룹 읽기를 허용한다.

```bash
chown root:1000 .env
chmod 640 .env
ls -l .env                        # -rw-r----- 1 root gemiso
```

`0600` 으로 두면 `docker compose logs app` 에 이렇게 남고 `Restarting (3)` 을 반복한다.

```
PermissionError: [Errno 13] Permission denied: '/app/.env'
```

현재 코드는 이 경우에도 기동은 하도록 고쳤다(compose 의 `env_file` 로 같은 값이
환경변수로 들어가기 때문). 하지만 경고가 남으므로 권한을 맞추는 것이 맞다.

---

## 9. 방화벽

```bash
firewall-cmd --permanent --add-port=19780/tcp
firewall-cmd --reload
firewall-cmd --list-ports          # 19780/tcp 확인
```

- 80 이 아니라 **19780** 이다. `--add-service=http` 로는 열리지 않는다.
- 다른 시스템이 쓰는 2943 등 기존 규칙은 건드리지 않는다.
- 컨테이너 안 nginx 는 계속 80 을 듣는다. `docker/nginx.conf` 는 수정하지 않는다.

---

## 10. 실행환경 이미지 빌드

이미지에는 **실행환경만** 들어간다. 애플리케이션 소스는 들어가지 않는다.

```
들어가는 것    Python 3.12 / pip 의존성 / gunicorn / curl / Claude Code CLI
안 들어가는 것 *.py, templates/, static/, .env, DB, 업로드 이미지
```

```bash
cd /data/chat-bot-v1/claude-web
docker compose build
docker images claude-web-runtime          # 디스크에서 약 600MB
```

빌드가 하는 일 (로그로 확인할 수 있다):

- Claude Code 공식 apt 저장소 등록 + **서명키 지문 검증**
  (`31DDDE24DDFAB679F42D7BD2BAA929FF1A7ECACE`. 다르면 빌드가 실패한다)
- `claude --version` 으로 설치 확인
- `requirements.txt` 설치 후 flask / werkzeug / gunicorn / cryptography 버전 확인
- `claudeweb` (uid 1000) 사용자 생성

인터넷이 안 되는 서버라면 개발 PC 에서 만들어 옮긴다.

```bash
# 개발 PC
docker compose build
docker save claude-web-runtime:1.0.0 | gzip > claude-web-runtime-1.0.0.tar.gz   # 약 170MB
# 서버
gunzip -c claude-web-runtime-1.0.0.tar.gz | docker load
docker images claude-web-runtime
```

> Claude CLI 를 올릴 때는 **이미지를 새 태그로 다시 빌드**한다.
> 컨테이너 안에서 자동 업데이트는 껐다(`DISABLE_AUTOUPDATER=1`).
> "이미지 태그 = Claude CLI 버전" 관계를 유지하기 위한 것이다.

---

## 11. 기동

```bash
cd /data/chat-bot-v1/claude-web
docker compose up -d
docker compose ps
```

기대 결과:

```
claude-web-init-1     Exited (0)          <- 1회성이다. 정상
claude-web-app-1      Up (healthy)
claude-web-nginx-1    Up (healthy)   0.0.0.0:19780->80/tcp
```

`healthy` 가 되기까지 30초 정도 걸린다(`start_period`). 그 사이의
`health: starting` 은 정상이다.

```bash
docker compose logs app | tail -30
curl -fsS http://127.0.0.1:19780/health && echo
docker compose exec app id           # uid=1000(claudeweb)  <- root 면 안 된다
docker compose exec app which claude # /usr/bin/claude
```

`init` 로그에 다음이 보이면 정상이다.

```
[entrypoint] chown -R 1000:1000 /var/lib/claude-web
```

---

## 12. Claude 최초 인증 (가장 중요)

컨테이너 안에서 한 번 로그인하면 `claude-web-home` 볼륨에 저장돼
**컨테이너를 지우거나 이미지를 바꿔도 유지된다.**

```bash
docker compose exec app claude
```

- 브라우저가 컨테이너의 콜백 주소로 돌아올 수 없으므로 **코드 붙여넣기 방식**을 쓴다.
  화면에 나오는 URL 을 PC 브라우저에서 열고, 받은 코드를 터미널에 붙여넣는다.
- 로그인이 끝나면 `/exit` 로 나온다.

확인:

```bash
docker compose exec app claude -p "Respond only with OK"     # OK
docker compose exec app ls -l /home/claudeweb/.claude/.credentials.json
#   -rw------- 1 claudeweb claudeweb   <- 0600, 소유자 claudeweb 이어야 한다
```

지속성 확인 (반드시 한 번 해볼 것):

```bash
docker compose down
docker compose up -d
docker compose exec app claude -p "Respond only with OK"     # 다시 로그인 없이 OK
```

> `docker compose down -v` 는 **운영에서 쓰지 않는다.** 인증 볼륨이 지워진다.
> `docker exec` 를 root 로 실행하면 root 소유의 인증 파일이 만들어져 웹이 읽지
> 못한다. compose 는 `user: claudeweb` 으로 고정해 두었으므로 위 명령을 그대로 쓴다.

---

## 13. 최초 관리자 계정

```bash
docker compose exec app python app.py create-admin
```

- 비밀번호는 화면에 보이지 않게 입력되고 해시로만 저장된다.
- 8자 이상. 이 계정이 관리자 페이지의 첫 진입 수단이다.

---

## 14. 관리자 페이지 설정

브라우저에서 `http://<서버IP>:19780/` → 로그인 → `/admin/claude`

| 항목 | 이 서버 값 |
|---|---|
| provider | `cli` |
| CLI 경로 | `/usr/bin/claude` |
| 작업 디렉터리 | `/var/lib/claude-web/workspace` |
| timeout | `180` |
| 동시 실행 | `3` |
| resume 사용 | 켬 |

**Connection Test** 를 눌러 CLI 가 발견되고 버전이 나오는지 확인한다.

> 이 값들은 DB 의 `settings` 테이블에 있다. `.env` 의 `CLAUDE_*` 는
> **settings 가 비어 있는 최초 1회만** 초기값으로 쓰인다. 이후에 `.env` 를 고쳐도
> 반영되지 않는다. 덕분에 **이미지를 바꾸거나 재빌드해도 이 설정은 유지된다.**

타임아웃은 이 순서를 지켜야 한다. 하나만 바꾸면 안 된다.

```
Claude 180  <  gunicorn 300  <  nginx proxy_read_timeout 360
(관리자 페이지)   (.env)          (docker/nginx.conf)
```

업로드 크기도 연결되어 있다.

```
MAX_UPLOAD_MB(10) x MAX_IMAGES_PER_MESSAGE(5) + 1MB = 51MiB
  -> docker/nginx.conf 의 client_max_body_size 52m
```

---

## 15. 동작 확인

```bash
curl -fsS http://127.0.0.1:19780/health && echo
curl -sI  http://127.0.0.1:19780/ | head -3
```

브라우저에서 확인할 것:

1. 로그인 / 로그아웃
2. 프로젝트 생성 (한글 이름)
3. 세션 생성 — private / public 각각
4. 메시지 전송 후 Claude 응답 수신
5. 이미지 첨부 전송 → 응답
6. 첨부 이미지 다시 열기
7. 다른 계정으로 로그인해 **남의 private 세션 URL 직접 입력 → 접근 거부**
8. 관리자 페이지 진입, 설정 변경 후 즉시 반영
9. 관리자 페이지에 API Key 가 평문으로 보이지 않는지
10. 세션 삭제 / 프로젝트 삭제
11. 재접속 시 로그인 유지
12. `docker compose restart app` 후 로그인 유지 (= `SECRET_KEY` 고정 확인)
13. `docker compose logs app` 에 API key 나 메시지 본문이 찍히지 않는지
14. 메모 생성 (제목 / 내용 / 공개 범위) → 재접속 후에도 남아 있는지
15. 메모에 파일 첨부 (png / pdf / txt) → 다시 열기
16. 메모 수정 / 첨부 추가 / 첨부 삭제 / 메모 삭제
17. public 메모를 다른 계정에서 "공유 메모" 로 읽을 수 있고 **수정은 막히는지**
18. 다른 사람의 private 메모 URL·첨부 URL 직접 입력 → 접근 거부
19. 10MB 넘는 파일 첨부 → 거부되고 화면이 깨지지 않는지
20. 관리자 Storage 탭에서 디스크/앱 데이터 값이 `df -h` `du -sh` 와 비슷한지
21. 일반 사용자로 `/admin/storage` 직접 입력 → 403

### PWA(홈 화면 추가)는 이 구성에서 동작하지 않는다

서비스워커는 **보안 컨텍스트(HTTPS 또는 localhost)** 에서만 등록된다.
평문 HTTP 로 서비스하는 동안은 포트와 무관하게 설치형 앱으로 동작하지 않는다.
앱은 등록 실패를 조용히 무시하도록 되어 있어 **다른 기능에는 영향이 없다.**
필요하면 사내 인증서로 HTTPS 를 붙인다 (`docker/nginx-https.conf.example`).

---

## 16. 재부팅 확인

```bash
systemctl is-enabled docker        # enabled
reboot
```

재접속 후:

```bash
cd /data/chat-bot-v1/claude-web
docker compose ps
curl -fsS http://127.0.0.1:19780/health && echo
docker compose exec app claude -p "Respond only with OK"
```

세 컨테이너가 자동으로 올라오고 Claude 인증도 유지되어야 한다.
별도의 systemd unit 은 만들지 않는다. `systemctl enable docker` +
`restart: unless-stopped` 로 충분하다.

---

## 17. 백업

```bash
cd /data/chat-bot-v1/claude-web
./scripts/docker-backup.sh
ls -l /home/claude-web-data/backups/
```

- DB 는 SQLite 온라인 백업(`python app.py backup`)으로 뜨고 `PRAGMA integrity_check`
  까지 확인한다. 서비스를 멈출 필요가 없다.
- 업로드 이미지는 tar 로, `.env` 는 `env-<시각>.bak` (0600) 으로 함께 보관된다.
- **Claude 인증정보는 일부러 백업하지 않는다.** 필요하면 수동으로 받는다.
  ```bash
  docker run --rm -v claude-web-home:/h -v "$PWD":/out alpine \
    tar czf /out/claude-home.tar.gz -C /h .
  ```
- 주기 실행 (예: 매일 03:10)
  ```bash
  crontab -e
  10 3 * * * cd /data/chat-bot-v1/claude-web && ./scripts/docker-backup.sh >> /var/log/claude-web-backup.log 2>&1
  ```

복구 절차는 `DEPLOYMENT_DOCKER.md` 16절에 있다.

---

## 18. 평소 운영

이 구조의 핵심은 **소스 / 데이터 / Claude 인증의 수명이 완전히 분리**되어 있다는 것이다.

```bash
cd /data/chat-bot-v1/claude-web
# 이번 메모/Storage 기능 반영 : 의존성 변경이 없으므로 재빌드 불필요
#   DB 마이그레이션(notes / note_attachments 추가)은 app 기동 시 자동 수행되고,
#   변경 전에 backups/ 에 자동 백업을 남긴다.
git pull && docker compose restart app
docker compose logs --tail=30 app | grep -i migrat


# 소스만 바뀐 경우  -> 재빌드 없음
git pull && docker compose restart app

# SSH 중계(feat/ssh-relay) 반영 : requirements.txt 가 안 바뀌었으므로 재빌드 불필요.
#   스키마 v9 마이그레이션은 기동할 때 자동으로 돌고, 먼저 backups/ 에 백업을 남긴다.
#   cryptography 는 원래부터 requirements.txt 에 있어 이미 이미지 안에 있다.
git pull && docker compose restart app
docker compose logs --tail=50 app | grep -i 'migrate\|백업'
docker compose exec app python -c "import cryptography; print(cryptography.__version__)"

# requirements.txt 가 바뀐 경우 -> 재빌드 필요
docker compose build app && docker compose up -d app

# 스크립트로 한 번에 (백업 -> pull -> 재기동 -> 확인)
./scripts/docker-update.sh
./scripts/docker-update.sh --build

# 상태 / 로그
docker compose ps
docker compose logs -f app
docker compose logs --tail=100 nginx

# DB 스키마 마이그레이션 (필요한 경우에만)
docker compose run --rm --no-deps app python app.py migrate

# 디스크
df -h / /home
du -sh /home/claude-web-data/*
docker system df
docker builder prune -f            # 빌드 캐시 회수 (수백 MB)
```

### 데이터가 어디에 쌓이는지

| 내용 | 위치 | 비고 |
|---|---|---|
| 대화 내용 (텍스트) | `/home/claude-web-data/chat.db` | SQLite |
| 채팅 첨부 이미지 | `/home/claude-web-data/uploads/` | 세션/프로젝트 삭제 시 함께 삭제됨 |
| 메모 (텍스트) | `/home/claude-web-data/chat.db` | `notes` 테이블 |
| 메모 첨부파일 | `/home/claude-web-data/notes/` | 메모 삭제 시 함께 삭제됨 |
| Claude 작업 디렉터리 | `/home/claude-web-data/workspace/` | |
| 중계 프로그램 (relay.exe) | `/home/claude-web-data/relay/` | 관리자가 올린 것 하나. git 에는 없다 |
| 백업 | `/home/claude-web-data/backups/` | 10개 유지 |
| Claude 인증 + 대화 기록 | Docker 볼륨 `claude-web-home` | `/var/lib/docker` 아래 = **`/` 에 있다** |
| 이미지 / 빌드 캐시 | `/var/lib/docker` | **`/` 에 있다** |

`/` 사용량 감시를 걸어두면 좋다.

```bash
crontab -e
0 8 * * * [ "$(df --output=pcent / | tail -1 | tr -dc 0-9)" -ge 85 ] && df -h / | mail -s "nfs-181 / 디스크 85% 초과" root
```

---

## 19. 문제 해결 (이 서버에서 실제로 났던 것)

### `Emulate Docker CLI using podman` 이 보인다 / 빌드 로그가 두 번 나온다

`docker` 가 podman 래퍼다. → [3절](#3-podman-정리-이-서버의-가장-중요한-단계)

```bash
rpm -qf "$(command -v docker)"
dnf remove -y podman-docker
```

### `failed to connect to the docker API at unix:///run/podman/podman.sock`

셸에 `DOCKER_HOST` 가 남아 있다. → [5절](#5-docker_host-잔존-값-정리)

```bash
unset DOCKER_HOST && docker context use default
```

### nginx 가 `bind: address already in use` 로 안 뜬다

host 의 포트가 이미 사용 중이다.

```bash
ss -ltnp | grep ':19780 '
grep '^HTTP_PORT=' .env
```

`.env` 의 `HTTP_PORT` 를 바꾼 뒤 `docker compose up -d` 를 다시 실행한다.
방화벽도 같이 열어야 한다. 32768 이상은 ephemeral 포트 범위와 겹치므로 피한다.

### app 이 `Restarting (3)` 을 반복한다

```bash
docker compose logs app | tail -40
```

| 로그 | 원인 | 조치 |
|---|---|---|
| `PermissionError: ... '/app/.env'` | `.env` 가 uid 1000 에게 읽기 불가 | `chown root:1000 .env && chmod 640 .env` |
| `PermissionError: ... chat.db` | 데이터 디렉터리 소유자 불일치 | `chown -R 1000:1000 /home/claude-web-data` |
| `SECRET_KEY 가 임시값` 경고 | `.env` 를 못 읽거나 값이 비었음 | 위 권한 확인 + `SECRET_KEY` 채우기 |
| `ModuleNotFoundError` | 이미지와 `requirements.txt` 불일치 | `docker compose build app && docker compose up -d app` |

### `docker compose logs init` 에 `/app/app.py 가 없습니다` 경고

실행 디렉터리가 잘못됐다. `/data/chat-bot-v1/claude-web` 에서 실행한다.
(`compose.yml` 은 `./` 를 `/app` 으로 마운트한다)

### 502 Bad Gateway

app 이 안 떠 있거나 아직 기동 중이다.

```bash
docker compose ps
docker compose logs --tail=50 app
```

app 컨테이너를 재생성해 IP 가 바뀌어도 nginx 는 따라간다
(`docker/nginx.conf` 가 `resolver 127.0.0.11` 로 매번 이름을 다시 찾는다).
nginx 를 재시작할 필요는 없다.

### `/admin` 으로 들어가면 포트가 사라진 주소로 이동한다

`docker/nginx.conf` 가 `Host $http_host` 를 넘기는지 확인한다.
`$host` 는 포트를 떼어내서 80 이 아닌 포트에서 리다이렉트가 깨진다. (현재 설정은 정상)

### Claude 가 `Not logged in` 이라고 한다

```bash
docker compose exec app ls -l /home/claudeweb/.claude/.credentials.json
docker compose exec app id      # uid=1000 이어야 한다
```

파일이 root 소유면 지우고 [12절](#12-claude-최초-인증-가장-중요)을 다시 한다.

---

## 20. 데이터 위치를 나중에 옮길 때

이미 `/var/lib/claude-web` 로 운영 중이라면 이 절차로 `/home` 으로 옮긴다.

```bash
cd /data/chat-bot-v1/claude-web
docker compose down                        # WAL 정리 후 정지

mkdir -p /home/claude-web-data
cp -a /var/lib/claude-web/. /home/claude-web-data/
ls -la /home/claude-web-data/              # 소유자가 1000:1000 으로 유지되어야 한다

sed -i 's|^HOST_DATA_DIR=.*|HOST_DATA_DIR=/home/claude-web-data|' .env
grep -n '^HOST_DATA_DIR=' .env

docker compose up -d
docker compose exec app ls -l /var/lib/claude-web/    # 컨테이너 안 경로는 그대로
docker inspect claude-web-app-1 --format '{{range .Mounts}}{{.Source}} -> {{.Destination}}{{"\n"}}{{end}}'
df -h / /home
```

**브라우저에서 기존 프로젝트·대화·이미지가 정상인지 확인한 뒤에** 이전 위치를 정리한다.

```bash
rm -rf /var/lib/claude-web
```

`.env` 의 `DATABASE_PATH` / `UPLOAD_DIR` / `BACKUP_DIR` 은 컨테이너 안 경로라
**고치지 않는다.** 백업 스크립트도 `HOST_DATA_DIR` 을 따라간다.

> `claude-web-home` 볼륨(Claude 인증 + 대화 기록)은 이 작업으로 옮겨지지 않고
> `/var/lib/docker` = `/` 에 남는다. 이것까지 옮기려면 compose 의 볼륨 정의를
> 바꿔야 한다.

---

## 부록 A. 복붙용 전체 순서

```bash
# --- 3. podman 래퍼만 제거 (담당자 확인 후) --------------------------------
dnf remove -y podman-docker
rpm -qa | grep -Ei 'podman'          # podman 본체는 남아야 한다

# --- 4. Docker CE ----------------------------------------------------------
dnf -y install dnf-plugins-core git
dnf config-manager --add-repo https://download.docker.com/linux/centos/docker-ce.repo
dnf -y install docker-ce docker-ce-cli containerd.io \
               docker-buildx-plugin docker-compose-plugin
systemctl enable --now docker

# --- 5. 잔존 DOCKER_HOST ---------------------------------------------------
unset DOCKER_HOST
docker context use default
docker info --format '{{.ServerVersion}}'

# --- 6. 소스 --------------------------------------------------------------
git clone https://github.com/readersun/chat-bot-v1.git /data/chat-bot-v1
cd /data/chat-bot-v1/claude-web

# --- 7. 데이터 디렉터리 ----------------------------------------------------
mkdir -p /home/claude-web-data
chown 1000:1000 /home/claude-web-data
chmod 0750 /home/claude-web-data

# --- 8. .env --------------------------------------------------------------
cp .env.example .env
python3 -c "import secrets;print(secrets.token_hex(32))"     # SECRET_KEY 에 넣는다
vi .env      # CLAUDE_WEB_IMAGE / HTTP_PORT=19780 / HOST_DATA_DIR / SECRET_KEY / CLAUDE_BIN
chown root:1000 .env
chmod 640 .env

# --- 9. 방화벽 ------------------------------------------------------------
firewall-cmd --permanent --add-port=19780/tcp && firewall-cmd --reload

# --- 10~11. 빌드 + 기동 ---------------------------------------------------
docker compose build
docker compose up -d
docker compose ps
curl -fsS http://127.0.0.1:19780/health && echo

# --- 12~13. Claude 로그인 + 관리자 계정 -----------------------------------
docker compose exec app claude                      # 코드 붙여넣기 방식으로 로그인, /exit
docker compose exec app claude -p "Respond only with OK"
docker compose exec app python app.py create-admin

# --- 14~15. 브라우저에서 설정과 기능 확인 ---------------------------------
#   http://<서버IP>:19780/  -> 로그인 -> /admin/claude -> Connection Test
```

## 부록 B. 체크리스트

- [ ] `rpm -qa | grep podman` — podman 본체와 다른 시스템 컨테이너가 살아 있다
- [ ] `docker --version` 이 podman 이 아니다
- [ ] `echo "[$DOCKER_HOST]"` 가 비어 있다
- [ ] `systemctl is-enabled docker` = enabled
- [ ] `pwd` = `/data/chat-bot-v1/claude-web`
- [ ] `/home/claude-web-data` 가 `1000:1000 0750`
- [ ] `.env` 가 `root:gemiso 0640`
- [ ] `.env` 에 `SECRET_KEY` 가 채워져 있다
- [ ] `.env` 의 `HTTP_PORT=19780`, `HOST_DATA_DIR=/home/claude-web-data`
- [ ] `firewall-cmd --list-ports` 에 19780/tcp
- [ ] `docker compose ps` — init Exited(0), app healthy, nginx healthy
- [ ] `docker compose exec app id` = uid 1000
- [ ] `docker compose exec app which claude` = `/usr/bin/claude`
- [ ] `claude -p "Respond only with OK"` 가 OK 를 준다
- [ ] `down` → `up` 후에도 로그인 없이 OK
- [ ] 관리자 계정 생성 + 관리자 페이지 Connection Test 성공
- [ ] 브라우저에서 이미지 첨부 대화 성공
- [ ] 남의 private 세션 URL 직접 접근이 차단된다
- [ ] `docker compose restart app` 후 로그인이 유지된다
- [ ] 재부팅 후 자동 기동 + Claude 인증 유지
- [ ] `./scripts/docker-backup.sh` 성공
- [ ] `df -h /` 여유 확인 + 감시 cron 등록
