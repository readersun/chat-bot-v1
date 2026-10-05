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
  │                │
  │  projects ─────┘            settings        audit_logs
  │
  └──owns──► notes ──► note_attachments
                  └──► note_comments (대댓글은 parent_id, 한 단계)
```

- **Project** = 세션을 묶는 공용 그룹. 관리자가 만든다.
- **Session** = 접근 권한의 단위. 만든 사람이 소유자이고 `private` / `public` 을 고른다.
- **Note** = 채팅과 별개인 메모. 세션과 같은 `private` / `public` 개념을 쓰지만
  **public 메모는 남이 읽기만 할 수 있다.** (세션은 함께 대화하므로 쓰기도 가능)
- **Comment** = 메모에 달린 댓글. **그 메모를 볼 수 있으면 누구나 쓸 수 있다.**
  본문과 달리 공개 메모에도 남이 댓글을 달 수 있다. 읽으라고 공개한 글에
  한 마디 남기는 것이 댓글의 쓸모이기 때문이다.
  고치는 것은 쓴 사람만, 지우는 것은 쓴 사람과 메모 작성자가 할 수 있다.
  답글(대댓글)은 한 단계까지만 달린다.

기능은 네 갈래다.

```
로그인
 ├─ 채팅 (/)          프로젝트 · 세션 · Claude 대화 · 세션 재개 · 이미지 첨부
 ├─ 메모 (/notes)     나만 보기 / 전체 공유 · 첨부파일 · 제목 검색 · 댓글과 답글
 ├─ 패치 (/patch)     고객사별 패치 저장소 열람과 내려받기
 └─ 서버 (/servers)   사내망 리눅스 서버에 붙기 (채팅으로 묻기 / 웹 터미널)

관리자 (/admin)
 ├─ 현황   ├─ 사용자   ├─ Claude 설정  ├─ 패치 설정
 ├─ 중계 설정          연결과 등록 · 사용 허용 · 기록
 ├─ 시스템 └─ 저장공간  서버 / 앱 저장공간 확인 (확인 전용, 삭제 기능 없음)
```

### 서버 (SSH 중계)

챗봇 서버는 사내망 리눅스 서버에 직접 붙지 못한다. VDI 에 올려 둔 중계
프로그램이 **이쪽으로 들어오는 방향 하나만** 써서 할 일을 받아 가고 결과를
올린다. 그래서 방화벽에 구멍을 내지 않는다.

```
[브라우저] ──► [챗봇 서버] ◄── POST /api/relay/poll ── [VDI: relay.exe] ──ssh──► [사내 서버]
                             (할 일 있으면 바로, 없으면 25초 기다림)
```

**중계는 사람마다 한 대다.** 쓰는 사람이 자기 VDI 에 깔고 자기 키로 서버에
붙는다. 일은 그 사람의 중계로만 나간다. 남의 중계가 붙어 있어도 내 일을 대신
해 주지 않는다. 그래서 서버 쪽 계정 권한이 그 사람 것 그대로다.

프로그램은 **관리자가 한 번 올리고 사용자가 웹에서 받는다.**

| 누가 | 어디서 | 무엇을 |
|---|---|---|
| 관리자 (한 번) | `relay_agent/build.cmd` | `relay.exe` 를 만든다 |
| 관리자 (한 번) | 운영 → 중계 설정 → 중계 프로그램 | 그 파일을 올린다 |
| 쓰는 사람 | 서버 → 내 중계 | 프로그램을 받고 등록 코드를 받는다 |
| 쓰는 사람 | VDI | `relay.exe register` 로 두 줄을 적는다 |

두 갈래로 쓴다.

- **채팅으로 묻기** — 말로 묻고 Claude 가 명령을 고른다. 조회 명령은 승인 없이
  돌고, 바꾸는 명령은 **승인 카드**가 뜬다. 사람이 누르기 전에는 나가지 않는다.
- **웹 터미널** — 내가 직접 친다. 사람이 친 명령에는 등급을 매기지 않는다.
  브라우저가 PuTTY 를 대신할 뿐이고 계정 권한이 늘지 않는다.

막는 겹이 셋이다. 서로 섞이지 않는다.

| 겹 | 무엇을 정하는가 | 어디에 |
|---|---|---|
| `servers` 메뉴 | 문이 열리는가 | `user_menus` |
| 등급 `ssh_level` | 조회만인가 변경까지인가 | `users.ssh_level` |
| 범위 `ssh_all_servers` | 어느 서버까지인가 | `users` / `ssh_grants` |

등급에는 천장이 하나 더 있다. 관리자 화면의 **기본 정책**(`relay_policy`)이다.
사람마다 '조회+변경' 을 받아 뒀어도 정책이 '조회만' 이면 변경 명령은 나가지
않는다. 둘 중 낮은 것이 이긴다. 정책 하나로 전사를 되돌릴 수 있어야 한다.

비밀이 어디에 있고 어디에 없는지:

| 무엇 | 어디에 | 비고 |
|---|---|---|
| 서버 비밀번호 | `ssh_servers.secret_enc` | SECRET_KEY 파생 Fernet. API Key 와 같은 방식 |
| 중계 토큰 | `relay_agents.key_hash` | sha256 만. 원문은 등록 때 한 번만 보여 준다 |
| 터미널에 친 키 | **어디에도 없다** | 메모리만 거쳐 중계로 간다 |
| 명령의 출력 | 그 대화(`messages`)에만 | 큐와 기록에는 넣지 않는다 |
| 저장 전 테스트의 비밀번호 | **어디에도 없다** | 메모리만 거쳐 중계로 간다 |

VDI 쪽 설치와 운영은 **[relay_agent/README.md](relay_agent/README.md)** 를 본다.
VDI 에서 손으로 적는 줄은 서버 주소와 등록 코드 두 개뿐이다.

## 구조

```
claude-web/
├── app.py               화면 + 채팅 API (진입점)
├── config.py            .env 기반 고정 설정
├── db.py                SQLite 스키마 / 마이그레이션 / 백업
├── settings_store.py    운영 설정(Claude 연결)을 DB 에서 관리
├── providers.py         Claude 호출 추상화 (CLI / API)
├── auth.py              로그인 / CSRF / rate limit / 최초 관리자
├── permissions.py       private·public 권한 규칙 (세션 + 메모. 단일 기준)
├── admin.py             관리자 페이지 + 관리 API
├── notes.py             개인/공유 메모 (화면 + API)
├── relay.py             SSH 중계 : 서버 목록 · 웹 터미널 · 중계 프로그램 API
├── relay_store.py       중계의 바닥 : 일 큐 · 터미널 버퍼 · 접속 정보 암·복호화
├── ssh_policy.py        챗봇이 고른 명령의 등급 (조회 / 변경 / 차단)
├── admin_relay.py       중계 설정 : 연결과 등록 · 사용 허용 · 기록
├── storage.py           저장공간 계산 (관리자 Storage 탭)
├── requirements.txt
├── .env.example
├── DEPLOYMENT.md        운영 배포 가이드 - OS 에 직접 설치
├── DEPLOYMENT_DOCKER.md 운영 배포 가이드 - Docker
├── DEPLOYMENT_SERVER_NFS181.md
│                     운영 서버 재설치 런북 (nfs-181 전용)
├── compose.yml          운영 Docker Compose (nginx + app)
├── compose.dev.yml      개발용 override (gunicorn --reload)
├── .dockerignore
├── docker/
│   ├── Dockerfile               실행환경 이미지 (소스는 넣지 않는다)
│   ├── entrypoint.sh            데이터 권한 정리 + 권한 강하
│   ├── nginx.conf               컨테이너 nginx (HTTP)
│   └── nginx-https.conf.example 컨테이너 nginx (사내 인증서 HTTPS)
├── deploy/
│   ├── claude-web.service       systemd 유닛 (비-Docker 배포)
│   ├── gunicorn.conf.py         운영 WSGI 설정 (워커 1개인 이유 포함)
│   ├── nginx-http.conf.example  host nginx - 내부망 HTTP
│   └── nginx-https.conf.example host nginx - 사내 인증서 HTTPS
├── scripts/
│   ├── install.sh       반복 작업 자동화 (Ubuntu, 비-Docker)
│   ├── backup.sh        DB online backup + uploads (비-Docker)
│   ├── docker-backup.sh Docker 배포용 백업
│   └── docker-update.sh Docker 배포용 업데이트
├── relay_agent/         VDI 에서 도는 중계 프로그램 (표준 라이브러리만)
│   ├── relay.py         register / status / run / unregister
│   ├── build.cmd        relay.exe 만들기 (관리자가 한 번, PyInstaller)
│   └── README.md        받기·등록·운영 (적는 줄은 서버 주소와 등록 코드 둘)
├── tests/               python -m unittest discover tests
│   ├── test_ssh_policy.py   명령 등급 (조회 / 변경 / 차단)
│   ├── test_relay_flow.py   중계 규약 · 터미널 · 승인 카드 · 기록
│   └── test_relay_guard.py  막는 자리 · 마이그레이션 · 청소
├── templates/
│   ├── index.html       채팅 화면 (반응형 + PWA)
│   ├── notes.html       메모 화면 (2단 / 모바일 1단)
│   ├── servers.html     서버 목록 (채팅 / 터미널 두 갈래)
│   ├── terminal.html    웹 터미널 (작은 ANSI 해석기 포함)
│   ├── login.html       로그인
│   ├── setup.html       최초 관리자 생성
│   ├── admin.html       관리자 (Dashboard / Users / Claude / System / Storage)
│   ├── admin_relay.html 중계 설정 (연결과 등록 / 사용 허용 / 기록)
│   └── error.html       403 / 404 등
├── static/
│   ├── type.css         @font-face 한 곳 (Pretendard)
│   ├── fonts/           내장 글꼴 + OFL 라이선스 원문
│   ├── shared.css       로그인·관리자·메모 공용 스타일
│   ├── manifest.webmanifest
│   ├── sw.js            서비스워커
│   └── icons/
└── data/                자동 생성
    ├── chat.db          SQLite (WAL)
    ├── relay/           올려 둔 중계 프로그램 (relay.exe + program.json)
    ├── backups/         마이그레이션 자동 백업
    ├── uploads/project_<pid>/session_<sid>/<uuid>.png      채팅 첨부
    └── notes/user_<uid>/note_<nid>/<uuid>.<ext>            메모 첨부
```

첨부파일은 DB 에 BLOB 으로 넣지 않는다. 디스크에 두고 DB 에는 메타데이터만
저장하며, 경로는 각 루트(`UPLOAD_DIR` / `NOTES_DIR`) 기준 **상대경로**로 적는다.
그래서 DB 를 다른 서버로 옮겨도 첨부가 그대로 열린다.

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

둘 다 **프로세스 메모리**에 있다. 그래서 gunicorn 워커를 여러 개로 늘리면
공유되지 않아 제한이 워커 수만큼 늘어나고, 같은 public 세션에 대한 동시 요청이
서로 다른 워커에 걸려 lock 을 통과해 버린다. 운영은 워커 1개 + 스레드 구성이다.

## DB 스키마

| 테이블 | 내용 |
|---|---|
| `users` | id, username(unique), password_hash(scrypt), display_name, role(admin/user), is_active, last_login_at |
| `settings` | key, value, updated_at, updated_by — 운영 설정 |
| `projects` | id, name, description |
| `sessions` | id, project_id, **owner_id**, name, **visibility**(private/public), claude_session_id |
| `session_members` | session_id, user_id, permission(read/write) — 향후 shared 용 |
| `messages` | id, session_id, **user_id**, role(user/assistant/error), content |
| `attachments` | id, session_id, message_id, original_name, stored_name, **file_path**(`UPLOAD_DIR` 기준 상대경로), mime_type, file_size |
| `audit_logs` | user_id, action, target_type, target_id, details |
| `login_attempts` | username, ip, success — 로그인 제한용 |
| `user_menus` | user_id, menu_key(chat/notes/patch/**servers**) — 문이 열리는가 |
| `ssh_servers` | name, host, port, username, auth_kind(key/password), key_name, **secret_enc**, is_enabled, last_check_* |
| `ssh_grants` | user_id, server_id — `users.ssh_all_servers = 0` 일 때만 본다 |
| `relay_agents` | **key_hash**(sha256), **owner_id**(사람마다 한 대), name, version, ip, scheme, last_seen_at, revoked_at |
| `relay_enroll_codes` | **code_hash**, expires_at, used_at — 한 번 쓰면 죽는다 |
| `relay_jobs` | kind(test/run/term_open/term_close), **owner_id**(그 사람 중계만 가져간다), state, payload, result — 전송 큐 |
| `term_sessions` | id, server_id, user_id, state, lines_in — 터미널 한 번 = 한 행 |
| `term_inputs` | term_id, line — 사람이 엔터로 끝낸 줄. 비밀번호 프롬프트 뒤는 `(가려짐)` |
| `ssh_commands` | server_id, session_id, message_id, command, level, state, approved_by, **result_note** |

`PRAGMA foreign_keys = ON`, `journal_mode = WAL`, 필요한 곳에 `ON DELETE CASCADE`.

기록용 표(`ssh_commands`, `term_sessions`, `term_inputs`)는 `ON DELETE SET NULL`
이다. 대화를 지운 사람이 그 대화에서 회사 서버에 나간 명령의 기록까지 지울 수
있으면 안 된다. 어느 대화였는지는 사라지지만 누가 언제 어느 서버에 무엇을
보냈는지는 남는다.

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

### 첨부 경로 상대화(v2 → v3)

v2 까지는 `attachments.file_path` 에 절대경로를 넣었다. 그 DB 를 다른 서버로
옮기면 업로드 경로가 달라져(`.../data/uploads` → `/var/lib/claude-web/uploads`)
첨부 API 의 경로 검사에 전부 걸려 이미지가 하나도 열리지 않는다.

v3 이 이 값들을 `UPLOAD_DIR` 기준 상대경로(`project_1/session_2/<uuid>.png`)로
바꾼다. 파일은 건드리지 않고 DB 의 표기만 옮긴다. 이후 저장도 상대경로라
경로가 바뀌어도 그대로 동작한다. 개발 PC 의 DB 를 운영 서버로 옮길 때 필요하다.

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
- **SSH 서버 비밀번호는 암호화할 수 없으면 저장하지 않는다.** API Key 와 다르게
  취급하는 이유는, 이것이 *다른 서버*로 들어가는 열쇠라서 DB 파일 하나가 새면
  그 서버까지 같이 넘어가기 때문이다. 서버 추가 화면에 "접속 정보는 암호화해서
  저장한다" 고 적어 두었으니, 조용히 평문으로 넣으면 적어 둔 말이 거짓이 된다.
  그래서 `cryptography` 가 없으면 `409` 로 거절한다. 키 인증은 열쇠가 중계 PC 에
  있고 DB 에는 이름만 남으므로 이 제약과 무관하게 쓸 수 있다.
  고치기 전에 들어간 평문이 남아 있으면 [운영] - [중계 설정] 이 몇 건인지 세어
  알려 준다. 해당 서버의 비밀번호를 다시 저장하면 암호화된다.
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

### 서버 (SSH 중계)

| 메서드 | 경로 | 권한 |
|---|---|---|
| GET | `/servers` | `servers` 메뉴 |
| GET | `/servers/<id>/terminal` | `servers` 메뉴 + 그 서버 허용 |
| GET | `/api/servers` | `servers` 메뉴 (범위 밖은 애초에 안 나온다) |
| POST/PATCH/DELETE | `/api/servers[/<id>]` | 관리자 |
| POST | `/api/servers/test` | 관리자 (저장 전 초안) |
| POST | `/api/servers/<id>/test` | 그 서버 허용 |
| GET | `/api/servers/<id>/sessions` | `servers` + `chat` 메뉴 |
| POST | `/api/sessions` | `chat` 메뉴 (서버를 붙여 대화를 만든다) |
| PATCH | `/api/sessions/<id>` | 소유자 (`server_id` 로 서버를 바꾼다) |
| GET | `/api/my-relay` | `servers` 메뉴 (내 중계 상태 + 프로그램 정보) |
| POST | `/api/my-relay/enroll` | `servers` 메뉴 (내 등록 코드. 한 번 쓰면 죽는다) |
| POST | `/api/my-relay/revoke` | `servers` 메뉴 (내 중계만 끊는다) |
| GET | `/servers/program` | `servers` 메뉴 (중계 프로그램 받기) |
| GET/POST | `/api/term` | `servers` 메뉴 + 그 서버 허용 |
| GET/POST | `/api/term/<tid>/io` | **내가 연 터미널만.** 관리자도 남의 화면은 못 본다 |
| POST | `/api/term/<tid>/close` | 내가 연 터미널만 |
| POST | `/api/ssh/commands/<id>/approve\|reject` | 그 대화에 쓸 수 있는 사람 |
| GET | `/admin/relay[/grants\|log]` | 관리자 |
| GET | `/api/admin/relay/summary\|grants\|log\|log.csv` | 관리자 |
| GET | `/api/admin/relay/log/term/<tid>` | 관리자 (사람이 친 줄 펼쳐 보기) |
| POST | `/api/admin/relay/revoke\|settings` | 관리자 |
| POST/DELETE | `/api/admin/relay/program` | 관리자 (중계 프로그램 올리기/내리기) |
| PUT | `/api/admin/relay/grants/<uid>` | 관리자 |
| POST | `/api/relay/register` | 등록 코드 (한 번 쓰면 죽는다) |
| POST | `/api/relay/poll\|result\|beat` | `X-Relay-Key` 헤더. 쿠키를 쓰지 않으므로 CSRF 대상이 아니다 |

`/api/relay/*` 는 **중계 프로그램 전용**이다. 이 접두사 아래는 CSRF 검사를
건너뛴다(쿠키를 쓰지 않으므로). 그래서 브라우저가 부르는 길은 절대 이 아래에
두지 않는다. 「내 중계」 화면이 쓰는 길이 `/api/my-relay/*` 로 따로 있는 이유다.

기록을 **지우거나 바꾸는 경로는 없다.** 화면에도 API 에도 없다. 기록을 손댈 수
있으면 기록이 아니다. 전송 큐(`relay_jobs`)의 끝난 행만 보관 기간 뒤에 자동으로
정리된다.

## 화면설계서

<https://claude.ai/code/artifact/0cb058fd-a9cf-4b39-97f7-1faab966fd71>

보드 15장. 화면·흐름·예외 화면과 API 표가 들어 있다. 코드를 고치면서 그림이
달라지면 **그림도 같이 고친다.** 틀린 그림이 남아 있으면 다음에 보는 사람이
그림을 믿는다.

## 시험

```
python -m unittest discover tests        # 전체 (약 4분, 90건)
python -m unittest tests.test_ssh_policy # 명령 등급만 (즉시)
```

진짜 ssh 를 띄우지 않는다. **가짜 중계**를 스레드로 돌리고, 그 가짜 중계는
진짜와 **같은 API** 를 쓴다. Claude 도 부르지 않는다. 미리 정한 답을 돌려주는
가짜 provider 를 끼우고, 그 답에 ` ```ssh ` 블록을 넣어 "챗봇이 명령을 고른
상황" 을 만든다.

눈여겨볼 시험 몇 가지.

| 시험 | 무엇을 막는가 |
|---|---|
| `test_no_probe_survives_in_the_database_file` | 현실적인 흐름을 한 번 돌린 뒤 **DB 파일의 바이트를 직접** 훑는다. 표를 하나씩 보는 시험은 새로 생긴 칸을 놓친다 |
| `test_password_is_encrypted_at_rest` | 조건을 달지 않는다. 전에는 `cryptography` 가 없으면 조용히 건너뛰어서, 전부 초록인데 암호화를 한 번도 검사하지 않았다 |
| `test_password_is_refused_when_it_cannot_be_encrypted` | 암호화 못 할 때 평문으로 저장되는 것 |
| `test_csrf_is_required_for_state_changes` | 브라우저가 부르는 길이 `/api/relay/` (CSRF 면제) 아래로 들어가는 것 |
| `test_my_relay_does_not_take_other_peoples_work` | 남의 중계가 내 일을 대신 하는 것 |
| `test_replacing_the_program_is_one_step` | 중계 프로그램을 바꿔 올릴 때 파일이 없는 순간이 생기는 것 |
| `test_filename_cannot_escape_or_inject` | 올린 파일 이름이 저장 경로나 응답 헤더로 새는 것 |

## 설치 / 실행

운영 배포 방법은 두 가지다. 둘 다 같은 소스, 같은 DB 스키마, 같은 데이터 경로를
쓰므로 서로 오갈 수 있다.

### A. Docker - **[DEPLOYMENT_DOCKER.md](DEPLOYMENT_DOCKER.md)**

OS 에는 Docker 와 git 만 설치하고, Python / gunicorn / Claude CLI 는 이미지에 둔다.
웹 소스는 이미지에 넣지 않고 host 의 git 작업 트리를 `/app` 으로 bind mount 한다.

```bash
cd /opt/claude-web/claude-web
cp .env.example .env && chmod 600 .env    # SECRET_KEY 기입
docker compose build
docker compose up -d
docker compose exec app claude            # Claude 최초 로그인
docker compose exec app python app.py create-admin
```

평소 운영:

```bash
git pull && docker compose restart app          # 소스만 변경
docker compose build app && docker compose up -d app   # requirements 변경
```

### B. OS 에 직접 설치 - **[DEPLOYMENT.md](DEPLOYMENT.md)**

아무것도 설치되지 않은 서버 한 대에 0부터 올리는 절차다.
(OS 패키지 -> 계정 -> 소스 -> venv -> Claude CLI -> 인증 -> .env -> DB -> gunicorn
-> systemd -> nginx -> 관리자 설정 -> 테스트 -> 재부팅 -> 업데이트 -> 백업 -> 장애 대응)

반복 작업만 자동화한 스크립트도 있다. 다만 Claude 인증과 관리자 설정은
사람이 직접 해야 하므로, 처음 배포한다면 DEPLOYMENT.md 를 읽고 진행할 것.

```bash
sudo bash scripts/install.sh
```

### 개발 PC 에서 띄울 때

```bash
cd claude-web
python3 -m venv venv
venv/bin/pip install -r requirements.txt
cp .env.example .env          # SECRET_KEY 만 채우면 된다
venv/bin/python app.py        # http://127.0.0.1:8080
```

`app.py` 를 직접 실행하면 Flask 개발 서버가 뜬다. **운영에는 쓰지 않는다.**

Claude CLI 확인 (웹 서버를 돌릴 계정으로):

```bash
which claude          # -> 관리자 페이지의 CLI 경로에 사용
claude --version
sudo -u claudeweb -H claude -p "Respond only with OK"
```

Claude 인증은 **계정 단위**(`$HOME/.claude/.credentials.json`)다. 본인 SSH 계정에서
되는 것으로는 부족하고, 서비스를 실행하는 계정에서 되어야 한다.

## 운영 WSGI (gunicorn)

```bash
venv/bin/gunicorn --config deploy/gunicorn.conf.py app:app
```

**워커는 반드시 1개다.** 동시 실행 제한(`ConcurrencyLimiter`), 세션 lock
(`_SESSION_LOCKS`), 최초 관리자 토큰(`auth._setup_token`)이 프로세스 메모리에
있어서 워커를 늘리면 공유되지 않고 조용히 깨진다. 동시성은 스레드로 낸다.
자세한 내용은 `deploy/gunicorn.conf.py` 주석과 DEPLOYMENT.md 12장에 있다.

타임아웃은 **Claude(180) < gunicorn(300) < nginx(360)** 순서를 지켜야 한다.

## systemd

```bash
sudo cp deploy/claude-web.service /etc/systemd/system/claude-web.service
sudo systemctl daemon-reload
sudo systemctl enable --now claude-web
sudo systemctl status claude-web
journalctl -u claude-web -f
```

`HOME` 과 `PATH` 를 unit 에 명시해야 한다. systemd 는 로그인 셸의 환경을
물려받지 않아서, 이게 빠지면 셸에서는 되는데 웹에서만 Claude 호출이 실패한다.

Docker 배포에서는 이 unit 을 쓰지 않는다. `systemctl enable --now docker` 와
compose 의 `restart: unless-stopped` 로 재부팅 후 자동 복구가 된다.

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
