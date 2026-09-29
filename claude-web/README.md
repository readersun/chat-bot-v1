# claude-web

사내에서 함께 쓰는 Claude Web Portal.
브라우저에서 로그인한 뒤 프로젝트/세션 단위로 Claude 와 대화한다.

```
브라우저 → 로그인 → Flask → Claude Provider → Claude → Flask → 브라우저
                                   ├── Claude CLI (claude -p)   ← 현재 기본
                                   └── Claude API (Messages API)
```

```
users ──owns──► sessions ──► messages ──► attachments
                   │
projects ──────────┘          settings        audit_logs
```

- **Project** = 세션을 묶는 공용 그룹. 관리자가 만든다.
- **Session** = 접근 권한의 단위. 만든 사람이 소유자이고 `private` / `public` 을 고른다.

## 구조

```
claude-web/
├── app.py               화면 + 채팅 API (진입점)
├── config.py            .env 기반 고정 설정
├── db.py                SQLite 스키마 / 마이그레이션 / 백업
├── settings_store.py    운영 설정(Claude 연결)을 DB 에서 관리
├── providers.py         Claude 호출 추상화 (CLI / API)
├── auth.py              로그인 / CSRF / rate limit / 최초 관리자
├── permissions.py       private·public 권한 규칙 (단일 기준)
├── admin.py             관리자 페이지 + 관리 API
├── requirements.txt
├── .env.example
├── claude-web.service   systemd 유닛 예시
├── templates/
│   ├── index.html       채팅 화면 (반응형 + PWA)
│   ├── login.html       로그인
│   ├── setup.html       최초 관리자 생성
│   ├── admin.html       관리자 (Dashboard / Users / Claude / System)
│   └── error.html       403 / 404 등
├── static/
│   ├── shared.css       로그인·관리자 공용 스타일
│   ├── manifest.webmanifest
│   ├── sw.js            서비스워커
│   └── icons/
└── data/                자동 생성
    ├── chat.db          SQLite (WAL)
    ├── backups/         마이그레이션 자동 백업
    └── uploads/project_<pid>/session_<sid>/<uuid>.png
```

## 배포와 Claude 설정의 분리

| 어디에 | 무엇을 | 언제 반영 |
|---|---|---|
| `.env` | 저장 경로, 포트, `SECRET_KEY`, 쿠키, 로그인 정책 | 서버 재시작 |
| 관리자 페이지 `/admin/claude` | provider, CLI 경로, workdir, extra args, timeout, 최대 동시 실행, API Key/모델 | **즉시** |

서버에 한 번 배포한 뒤로는 Claude 관련 설정을 바꾸려고 파일을 고치거나
서비스를 재시작할 필요가 없다. `.env` 의 `CLAUDE_*` 값은 settings 테이블이
비어 있는 최초 1회에만 초기값으로 들어간다.

```
배포 → 서비스 실행 → /setup 으로 관리자 생성 → /admin/claude 에서 연결 설정
     → [Claude 연결 테스트] → /admin/users 에서 계정 생성 → 사용자 로그인
```

## 권한 규칙

`permissions.py` 한 곳에만 있고 모든 API 가 이 함수를 쓴다.
프론트엔드에서 숨기는 것으로 끝내지 않고 **서버에서 반드시 다시 검사**한다.

| | 조회 | 작성 | 이름/공개범위 변경 | 삭제 |
|---|:--:|:--:|:--:|:--:|
| private · 소유자 | O | O | O | O |
| private · 그 외 | **X** | X | X | X |
| public · 소유자 | O | O | O | O |
| public · 다른 로그인 사용자 | O | **O** | X | X |
| 비로그인 | X | X | X | X |

- private 세션은 id 를 직접 입력해도 `404` 다. 존재 여부조차 알려주지 않는다.
- 첨부 이미지(`GET /api/attachments/<id>`)도 같은 규칙으로 검사한다.
  URL 을 알아도 권한이 없으면 받을 수 없다.
- **관리자도 private 대화 내용은 볼 수 없다.** "나만 보기"가 실제로 나만 보기여야
  하기 때문이다. 관리자는 메타데이터(목록/소유자/메시지 개수)만 다룬다.
- 프로젝트 생성/수정/삭제는 관리자만. 세션 생성은 로그인 사용자 누구나.

향후 "특정 사용자에게만 공유(shared)"를 붙일 수 있도록 `session_members`
(session_id, user_id, permission) 테이블과 권한 함수는 이미 준비해 두었다.
규칙을 바꿀 때 고칠 파일은 `permissions.py` 하나다.

## Claude Provider

```python
class ClaudeProvider:
    def supports_resume(self): ...
    def send(self, question, images=(), history=(), resume_id=None, new_session_id=None): ...
    def describe(self): ...   # 관리자 화면 상태 표시
    def test(self): ...       # 연결 테스트
```

채팅 로직은 이 인터페이스에만 의존한다.

- **ClaudeCliProvider** (기본) — `claude -p --output-format json`
  `--session-id` / `--resume` 로 세션 문맥을 잇는다.
- **ClaudeApiProvider** — Anthropic Messages API. 표준 라이브러리 `urllib` 만 쓴다.
  API 에는 세션 resume 이 없으므로 `supports_resume()` 이 `False` 이고,
  그러면 호출부가 DB 의 최근 대화를 `history` 로 넘긴다. **채팅 코드는 그대로다.**

### CLI 인자 주의점

`--add-dir` 는 가변 인자(`<directories...>`)라서 **바로 뒤에 플래그가 와야 한다.**
그렇지 않으면 마지막 프롬프트까지 디렉터리로 먹어서
`Input must be provided either through stdin or as a prompt argument` 로 실패한다.

```
claude [extra args] -p --add-dir <업로드루트> --output-format json \
       (--resume <uuid> | --session-id <uuid>) "<프롬프트>"
```

`shell=True` 는 쓰지 않고 argument list 로만 실행한다.

### 이미지 전달 방식

설치된 CLI(2.1.263)에는 이미지 전용 옵션이 없다. 그래서

1. 서버가 이미지를 `UPLOAD_DIR` 아래 UUID 이름으로 저장하고
2. 프롬프트에 **절대경로**를 적어주고
3. `--add-dir` 로 그 경로 접근을 허용해

Claude 가 자체 Read 도구로 읽게 한다. API provider 를 쓰면 같은 이미지를
base64 content block 으로 바꿔 보낸다. 이 차이는 provider 안에 갇혀 있다.

## 동시 실행

- **세션 단위 lock** — 같은 세션에 동시에 요청이 들어가면 Claude 문맥이 꼬인다.
  한 건만 처리하고 나머지는 `409 Busy`. public 세션에서 특히 중요하다.
- **전역 한도** — `최대 동시 실행`(관리자 설정)을 넘으면 `429`. 값을 바꾸면
  재시작 없이 다음 요청부터 적용된다.

## DB 스키마

| 테이블 | 내용 |
|---|---|
| `users` | id, username(unique), password_hash(scrypt), display_name, role(admin/user), is_active, last_login_at |
| `settings` | key, value, updated_at, updated_by — 운영 설정 |
| `projects` | id, name, description |
| `sessions` | id, project_id, **owner_id**, name, **visibility**(private/public), claude_session_id |
| `session_members` | session_id, user_id, permission(read/write) — 향후 shared 용 |
| `messages` | id, session_id, **user_id**, role(user/assistant/error), content |
| `attachments` | id, session_id, message_id, original_name, stored_name, file_path, mime_type, file_size |
| `audit_logs` | user_id, action, target_type, target_id, details |
| `login_attempts` | username, ip, success — 로그인 제한용 |

`PRAGMA foreign_keys = ON`, `journal_mode = WAL`, 필요한 곳에 `ON DELETE CASCADE`.

## 마이그레이션

기존 DB 를 **지우지 않는다.** 필요한 테이블/컬럼만 추가한다.

```bash
python app.py migrate     # 명시적으로 실행 (서버 기동 시에도 자동 수행)
python app.py backup      # 수동 백업 (data/backups/)
```

- 변경이 필요하고 데이터가 있을 때만 자동으로 먼저 백업한다.
  (파일 복사가 아니라 sqlite 온라인 백업 API 를 쓰므로 WAL 에서도 일관적이다)
- 모든 DDL/DML 이 한 트랜잭션이다. 중간에 실패하면 통째로 롤백되고 원본이 남는다.
- `PRAGMA user_version` 으로 적용 여부를 기록하며 여러 번 실행해도 안전하다.

### 로그인 도입(v1 → v2) 시 기존 데이터 처리

v1 에는 로그인이 없었다. 즉 기존 세션은 "서버에 접근할 수 있는 모두가 보던 것"이다.
그래서 기존 세션을 이렇게 옮긴다.

```
owner_id   = NULL    (레거시. 작성자를 알 수 없음)
visibility = public  (기존과 똑같이 모두가 볼 수 있음)
```

임의의 admin 을 소유자로 지정하면 (1) 쓰지 않은 대화의 소유자가 되고
(2) private 로 바뀌면 이전에 보던 사람들이 못 보게 된다. 그래서 **기존 동작을
그대로 보존하는** 쪽을 택했다.

소유자가 없는 세션은 일반 사용자가 이름 변경/삭제를 할 수 없다.
관리자 대시보드에 목록이 뜨고 거기서 **소유자를 지정**하면 그때부터 그 사람이 관리한다.

## 최초 관리자

`users` 가 비어 있을 때만 `/setup` 이 열린다. 다만 "먼저 접속한 사람이 관리자가
되는" 경쟁을 막기 위해 서버 기동 시 만든 **1회용 bootstrap 토큰**을 함께 요구한다.
서버 셸에 접근할 수 있는 사람만 관리자를 만들 수 있다는 뜻이다.

```bash
journalctl -u claude-web -n 50 | grep -i "bootstrap token"
cat /var/lib/claude-web/setup-token.txt        # 0600
```

셸에서 바로 만들 수도 있다. (자동화하려면 표준입력으로 넘기면 된다)

```bash
sudo -u claude -H /opt/claude-web/venv/bin/python /opt/claude-web/app.py create-admin
```

관리자가 하나라도 생기면 `/setup` 은 404 가 되고 토큰 파일은 삭제된다.

## 보안

- 비밀번호는 werkzeug scrypt 해시로만 저장한다. 평문은 DB·로그·감사기록 어디에도 없다.
- 인증 수단은 서명된 세션 쿠키뿐이다. `HttpOnly`, `SameSite=Lax`,
  HTTPS 면 `SESSION_COOKIE_SECURE=1`. localStorage 는 UI 상태(`last_project_id`,
  `last_session_id`, `session_scope`)만 담는다.
- 비밀번호를 바꾸면 쿠키에 심어둔 지문이 달라져 **다른 기기의 세션이 무효화**된다.
- CSRF: 세션에 저장한 토큰을 `X-CSRF-Token` 헤더 또는 `_csrf` 폼 필드로 다시 받는다.
  GET 이외의 모든 요청에 적용된다.
- 로그인 제한: 계정 기준 5회 / 10분. IP 기준은 그 4배(20회).
  사내 NAT·프록시 뒤에서 한 사람의 오타로 전원이 잠기지 않게 일부러 느슨하다.
- 업로드: 확장자 + 매직바이트 + 선언 MIME 세 가지를 모두 검사하고 UUID 로 저장한다.
  사용자가 준 파일명은 저장 경로 계산에 쓰지 않으므로 `../../etc/passwd` 가 와도
  경로가 바뀌지 않는다. 첨부는 DB id 로만 조회하며 파일 경로를 URL 로 받지 않는다.
- API Key 는 화면에 다시 표시하지 않는다(`sk-ant-****1234` 형태의 마스킹만).
  `cryptography` 가 설치되어 있으면 `SECRET_KEY` 파생 키로 암호화해 저장하고,
  없으면 평문으로 저장하되 DB 파일 권한을 0600 으로 제한한다. 오류 메시지와 로그에서는
  키 패턴을 지운다.
- 응답에 `X-Content-Type-Options`, `X-Frame-Options`, `Referrer-Policy` 를 붙이고
  `/api/*`·`/admin/*`·`/login` 은 `no-store` 로 내려보낸다.
- **`claude -p` 는 서버에서 실제 명령을 실행할 수 있다.** 로그인을 붙였더라도
  계정이 있는 사람은 서버에서 명령을 돌릴 수 있는 것과 같다.
  신뢰된 네트워크에만 노출하고 계정 발급을 통제하라.

## PWA

- 홈 화면에 추가해 앱처럼 쓸 수 있다. (Android/Chrome: 헤더의 `앱 설치`,
  iOS Safari: 공유 → 홈 화면에 추가)
- 서비스워커는 **인증된 내용을 캐시하지 않는다.** `/api/*`, `/admin/*`,
  `/login`, `/logout`, `/setup` 은 물론 앱 셸 `/` 도 캐시하지 않는다.
  로그인 후의 `/` 응답에는 사용자 이름과 CSRF 토큰이 들어가므로, 캐시하면 같은
  기기를 쓰는 다른 사람에게 노출될 수 있다. 캐시 대상은 아이콘·CSS·manifest 뿐이다.
- 서비스워커는 HTTPS 또는 localhost 에서만 등록된다.

## UI

| 화면 폭 | 레이아웃 |
|---|---|
| ~899px | 채팅 1단 + 햄버거 드로어(프로젝트·세션·필터·검색) |
| 900~1199px | 내비 + 채팅 2단 |
| 1200px~ | 프로젝트 / 세션 / 채팅 3단 |

- 세션 목록에 `🔒`(나만 보기) / `🌐`(전체 공개) 아이콘과 소유자 이름을 표시한다.
- `[전체] [내 세션] [전체 공개]` 필터와 이름 검색(LIKE)을 제공한다.
- 공개 세션에서는 메시지마다 작성자 이름이 보이고, Claude 에도 누가 한 말인지
  알려준다. (DB 에는 원문 그대로 저장한다)
- 관리 버튼(공개범위·이름·삭제)은 소유자에게만 렌더링되고, 서버도 동일하게 막는다.

## API

| 메서드 | 경로 | 권한 |
|---|---|---|
| GET | `/` | 로그인 |
| GET/POST | `/login`, `/logout`, `/setup` | - |
| GET | `/health` | - |
| GET | `/api/me` | 로그인 |
| POST | `/api/me/password` | 로그인 |
| GET | `/api/projects` | 로그인 |
| POST/PATCH/DELETE | `/api/projects[/<id>]` | 관리자 |
| GET | `/api/projects/<pid>/sessions?scope=all\|mine\|public&q=` | 로그인 (권한 필터) |
| POST | `/api/projects/<pid>/sessions` | 로그인 |
| GET | `/api/sessions/<sid>` | 조회 권한 |
| PATCH/DELETE | `/api/sessions/<sid>` | 소유자 |
| GET | `/api/sessions/<sid>/messages` | 조회 권한 |
| POST | `/api/sessions/<sid>/messages` | 작성 권한 (json 또는 multipart) |
| GET | `/api/attachments/<id>` | 해당 세션 조회 권한 |
| GET | `/admin[/dashboard\|users\|claude\|system]` | 관리자 |
| GET | `/api/admin/summary`, `/api/admin/audit` | 관리자 |
| GET/POST | `/api/admin/settings` | 관리자 |
| POST | `/api/admin/settings/<key>/clear` | 관리자 |
| POST | `/api/admin/claude/test` | 관리자 |
| GET/POST | `/api/admin/users` | 관리자 |
| PATCH/DELETE | `/api/admin/users/<id>` | 관리자 |
| POST | `/api/admin/users/<id>/password` | 관리자 |
| GET | `/api/admin/sessions` | 관리자 (메타데이터만) |
| PATCH/DELETE | `/api/admin/sessions/<id>` | 관리자 |

## 설치 / 실행

리눅스 서버 기준이다. 저장소 루트가 아니라 그 안의 `claude-web/` 이 앱 디렉터리이므로
심볼릭 링크로 `/opt/claude-web` 을 만들어 두면 `git pull` 만으로 갱신할 수 있다.

```bash
# 1) 서비스 계정. Claude CLI 인증은 계정별 ~/.claude 에 저장되므로 전용 계정을 쓴다.
sudo useradd -m -d /home/claude -s /bin/bash claude

# 2) 코드 배치
sudo git clone https://github.com/readersun/chat-bot-v1.git /opt/chat-bot-v1
sudo ln -s /opt/chat-bot-v1/claude-web /opt/claude-web
sudo chown -R claude:claude /opt/chat-bot-v1

# 3) 가상환경과 설정 (모두 claude 계정으로)
sudo -u claude -H bash -lc '
  cd /opt/claude-web
  python3 -m venv venv
  venv/bin/pip install --upgrade pip
  venv/bin/pip install -r requirements.txt
  cp .env.example .env
'

# 4) .env 수정 (최소한 SECRET_KEY 는 반드시 채운다)
sudo -u claude -H vi /opt/claude-web/.env

# 5) 수동 기동 확인
sudo -u claude -H /opt/claude-web/venv/bin/python /opt/claude-web/app.py
```

Claude CLI 확인 (웹 서버를 돌릴 계정으로):

```bash
which claude          # -> 관리자 페이지의 CLI 경로에 사용
claude --version
sudo -u claude -H claude -p "hello"
```

코드 갱신:

```bash
sudo -u claude -H git -C /opt/chat-bot-v1 pull
sudo systemctl restart claude-web
```

## systemd

```bash
sudo cp claude-web.service /etc/systemd/system/claude-web.service
sudo systemctl daemon-reload
sudo systemctl enable --now claude-web
sudo systemctl status claude-web
journalctl -u claude-web -f
```

## 환경변수 (.env)

| 이름 | 기본값 | 설명 |
|---|---|---|
| `DATABASE_PATH` | `data/chat.db` | SQLite 경로 |
| `UPLOAD_DIR` | `data/uploads` | 업로드 저장 경로 |
| `BACKUP_DIR` | `<DB폴더>/backups` | 마이그레이션 자동 백업 |
| `SECRET_KEY` | (임시 랜덤) | **반드시 지정.** 비우면 재시작마다 전원 로그아웃 |
| `HOST` / `PORT` | `0.0.0.0` / `8080` | 바인딩 |
| `SESSION_COOKIE_SECURE` | `0` | HTTPS 면 `1` |
| `SESSION_LIFETIME_DAYS` | `14` | 로그인 유지 기간 |
| `TRUST_PROXY` | `0` | 프록시 뒤일 때만 `1` |
| `MIN_PASSWORD_LENGTH` | `8` | 비밀번호 최소 길이 |
| `LOGIN_MAX_FAILURES` / `LOGIN_WINDOW_MINUTES` | `5` / `10` | 계정 기준 로그인 제한 |
| `LOGIN_IP_FACTOR` | `4` | IP 기준 한도 배수 |
| `MAX_UPLOAD_MB` / `MAX_IMAGES_PER_MESSAGE` | `10` / `5` | 업로드 제한 |
| `MAX_INPUT_CHARS` | `8000` | 입력 길이 제한 |
| `MAX_HISTORY_MESSAGES` / `MAX_HISTORY_CHARS` | `16` / `12000` | resume 불가 시 프롬프트에 넣을 범위 |
| `CLAUDE_*`, `MAX_CONCURRENT_CLAUDE` | - | **최초 1회 초기값.** 이후 `/admin/claude` |

## 운영 메모

- **gunicorn 을 쓴다면 워커는 1개여야 한다.** 세션 lock 과 동시 실행 카운터가
  프로세스 메모리에 있어서 워커가 여러 개면 무력화된다.
  `gunicorn -w 1 --threads 8 -b 0.0.0.0:8080 app:app`
- nginx 뒤에 둘 때: `proxy_read_timeout 300s;`, `client_max_body_size 60M;`,
  그리고 `.env` 에 `TRUST_PROXY=1`.
- 데이터 초기화: 서비스를 멈추고 `data/chat.db*` 와 `data/uploads/` 를 지운 뒤
  다시 기동하면 `/setup` 부터 시작한다. 백업은 `data/backups/` 에 남는다.
- 감사 로그는 `/admin/system` 하단에서 볼 수 있다. 로그인, 계정/권한 변경,
  설정 변경, 프로젝트·세션 삭제, 공개범위 변경만 기록하고 **대화 내용과
  비밀번호·API Key 는 기록하지 않는다.**
