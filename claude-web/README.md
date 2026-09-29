# claude-web

브라우저에서 질문을 입력하면 Flask 서버가 로컬 Claude Code CLI(`claude -p`)를 실행하고
그 결과를 화면에 보여주는 웹 챗봇. 로그인 없이 모든 사용자가 같은 목록을 공유한다.

```
브라우저 → Flask 웹 서버 → claude -p → Claude → Flask → 브라우저
```

```
Project
  └── Session  (= Claude CLI 세션 1개)
        ├── Messages
        └── Attachments (이미지)
```

## 구조

```
claude-web/
├── app.py
├── requirements.txt
├── .env.example
├── claude-web.service          # systemd 유닛 예시
├── templates/
│   └── index.html              # 반응형 UI (CSS Grid + 드로어)
├── static/                     # PWA
│   ├── manifest.webmanifest
│   ├── sw.js                   # 서비스워커
│   └── icons/                  # 192 / 512 / maskable / apple-touch
└── data/                       # 자동 생성
    ├── chat.db                 # SQLite (WAL)
    └── uploads/project_<pid>/session_<sid>/<uuid>.png
```

## UI 레이아웃

CSS Grid 기반 3단계 반응형. 브레이크포인트는 두 개뿐이다.

| 화면 폭 | 레이아웃 |
|---|---|
| `< 900px` (모바일) | 대화 화면만 표시. ☰ 로 여는 드로어에 프로젝트+세션 (폭 `min(86vw, 400px)`, 바깥 탭/ESC 로 닫힘) |
| `900–1199px` (태블릿) | 프로젝트+세션을 하나의 사이드바(300px)로 합치고 + 대화 (2단) |
| `≥ 1200px` (데스크톱) | 프로젝트(240px) \| 세션(280px) \| 대화 (3단) |

같은 DOM 으로 세 레이아웃을 만든다. 데스크톱에서는 `#nav { display: contents }` 로
두 패널을 그리드의 직접 자식으로 승격시키고, 모바일에서는 `position: fixed` 드로어가 된다.

레이아웃 토큰은 `:root` 에 모아 두었다 — `--project-w`, `--session-w`, `--nav-w`,
`--header-h`, `--composer-max`, `--tap`(44px 터치 타겟), `--app-h`, `--sa-*`(safe area).

모바일 대응 요점:

- 높이는 `100dvh` 기준이며, JS 가 `visualViewport` 높이로 `--app-h` 를 덮어써
  **키보드가 올라와도 입력창이 화면 안에 남는다** (iOS Safari 포함)
- `env(safe-area-inset-*)` 로 노치/홈 인디케이터 영역 회피 (`viewport-fit=cover`)
- 페이지 전체 가로 스크롤 차단. 긴 코드/로그는 말풍선 안에서만 가로 스크롤
- textarea 는 내용에 따라 자동 증가 → `--composer-max`(168px)에서 멈추고 내부 스크롤
- 첨부/전송 버튼은 44px 고정, 어떤 상태에서도 화면 안에 유지

## PWA

- `/manifest.webmanifest` — standalone, scope `/`, 192·512·maskable 아이콘
- `/sw.js` — 루트 스코프로 제공 (`Service-Worker-Allowed: /`)
  - `/api/*` 와 `/health` : **캐시하지 않음** (대화 데이터는 항상 서버에서)
  - 페이지 이동 : 네트워크 우선 → 실패 시 캐시된 앱 셸 → 그래도 실패하면 오프라인 안내
  - 정적 리소스 : 캐시 우선 + 백그라운드 갱신
- Android/데스크톱 Chrome 에서는 헤더의 `앱 설치` 버튼(`beforeinstallprompt`),
  iOS Safari 는 공유 → 홈 화면에 추가
- 서비스워커는 HTTPS 또는 localhost 에서만 등록된다. 사내 IP(`http://192.168.x.x:8080`)로
  접속하면 앱 자체는 정상 동작하지만 설치/오프라인 기능은 동작하지 않는다.
  필요하면 앞단에 HTTPS 리버스 프록시를 두면 된다.

## DB 스키마

| 테이블 | 컬럼 |
|---|---|
| `projects` | id, name, description, created_at, updated_at |
| `sessions` | id, **project_id→projects.id**, name, claude_session_id, created_at, updated_at |
| `messages` | id, **session_id→sessions.id**, role(user/assistant/error), content, created_at |
| `attachments` | id, **session_id→sessions.id**, **message_id→messages.id**, original_name, stored_name, file_path, mime_type, file_size, created_at |

모든 FK 는 `ON DELETE CASCADE`. 접속마다 `PRAGMA foreign_keys = ON`, DB 는 `journal_mode = WAL`.

## Claude 세션 유지

Claude Code CLI 2.1.263 기준 실제 지원 옵션만 사용한다.

```bash
claude -p --output-format json --session-id <uuid> "첫 질문"   # 세션 생성
claude -p --output-format json --resume <uuid>     "다음 질문"   # 문맥 이어가기
```

- 웹 세션 1개 = Claude 세션 1개 (`sessions.claude_session_id`)
- 서버를 재시작해도 `--resume` 으로 그대로 이어진다
- resume 이 실패하면 새 세션을 만들고 DB 의 최근 메시지를 프롬프트에 넣어 자동 복구한다
  (`CLAUDE_USE_RESUME=0` 으로 이 fallback 방식만 쓰게 할 수도 있다)

## 이미지 전달 방식

Claude Code CLI 에는 이미지 전용 옵션이 없다(`--file` 은 원격 file_id 용). 따라서
**서버에 저장한 이미지의 절대경로를 프롬프트에 적어주고 Claude 가 자체 Read 도구로 읽게** 한다.
업로드 폴더는 `--add-dir` 로 접근을 허용한다.

```bash
claude -p --output-format json --add-dir /opt/claude-web/data/uploads --resume <uuid> \
"첨부된 이미지 파일 2개:
1. error.png -> /opt/claude-web/data/uploads/project_1/session_4/a1b2c3....png
2. screen.jpg -> /opt/claude-web/data/uploads/project_1/session_4/d4e5f6....jpg

위 이미지 파일을 Read 도구로 열어서 확인한 뒤 답해줘.

사용자 질문:

이 두 화면의 차이가 뭐야?"
```

모든 실행은 `subprocess.run()` 의 argument list 방식이며 `shell=True` 를 쓰지 않는다.

## API

| 메서드 | 경로 | 설명 |
|---|---|---|
| GET | `/` | 채팅 화면 |
| GET | `/health` | `{"status":"ok"}` |
| GET | `/manifest.webmanifest` | PWA manifest |
| GET | `/sw.js` | 서비스워커 (루트 스코프) |
| GET | `/api/projects` | 프로젝트 목록 |
| POST | `/api/projects` | 생성 `{name, description}` |
| PATCH | `/api/projects/<id>` | 이름/설명 수정 |
| DELETE | `/api/projects/<id>` | 삭제 (하위 전부 + 업로드 파일) |
| GET | `/api/projects/<pid>/sessions` | 세션 목록 |
| POST | `/api/projects/<pid>/sessions` | 세션 생성 (기본 이름 `새 대화`) |
| GET | `/api/sessions/<id>` | 세션 조회 |
| PATCH | `/api/sessions/<id>` | 이름 변경 / `reset_claude_session` |
| DELETE | `/api/sessions/<id>` | 삭제 (메시지·첨부·파일) |
| GET | `/api/sessions/<id>/messages` | 메시지 + 첨부 목록 |
| POST | `/api/sessions/<id>/messages` | 질문 전송 (multipart: `message`, `images` 복수) |
| GET | `/api/attachments/<id>` | 이미지 제공 (DB id 로만 조회) |
| POST | `/chat`, `/clear` | 구버전 호환용 단일 세션 API |

## 설치 / 실행

```bash
sudo mkdir -p /opt/claude-web && sudo chown -R claude:claude /opt/claude-web
cd /opt/claude-web
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip && pip install -r requirements.txt
cp .env.example .env && vi .env
python app.py
```

접속: `http://<서버IP>:8080/`

Claude CLI 확인 (웹 서버를 돌릴 계정으로):

```bash
which claude          # -> CLAUDE_BIN 에 사용
claude --version
sudo -u claude -H claude -p "hello"
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

| 변수 | 기본값 | 설명 |
|---|---|---|
| `CLAUDE_BIN` | `claude` | CLI 경로 |
| `CLAUDE_WORKDIR` | (실행 디렉터리) | `claude -p` 의 cwd |
| `CLAUDE_TIMEOUT` | `180` | 프로세스 타임아웃(초) |
| `CLAUDE_USE_RESUME` | `1` | 0이면 항상 DB 기록 프롬프트 방식 |
| `CLAUDE_EXTRA_ARGS` | (없음) | 예: `--model sonnet` |
| `DATABASE_PATH` | `./data/chat.db` | SQLite 파일 |
| `UPLOAD_DIR` | `./data/uploads` | 업로드 루트 |
| `MAX_UPLOAD_MB` | `10` | 이미지 1장 최대 크기 |
| `MAX_IMAGES_PER_MESSAGE` | `5` | 메시지당 최대 이미지 수 |
| `MAX_CONCURRENT_CLAUDE` | `3` | 전체 동시 claude 프로세스 |
| `MAX_INPUT_CHARS` | `8000` | 입력 1건 최대 길이 |
| `MAX_HISTORY_MESSAGES` / `MAX_HISTORY_CHARS` | `16` / `12000` | fallback 프롬프트 범위 |
| `HOST` / `PORT` | `0.0.0.0` / `8080` | 바인딩 |
| `SECRET_KEY` | 랜덤 | 세션 쿠키 서명 |

## 보안 메모

- `shell=True` 미사용, `subprocess.run()` argument list 방식
- 업로드: 확장자 + 매직바이트 + MIME 3중 검증, 저장명은 UUID (원본명은 DB 에만)
  → `../../etc/passwd.png` 같은 이름이 와도 경로에 영향 없음
- 이미지 제공은 DB id 로만 (`/api/attachments/<id>`), UPLOAD_DIR 밖 경로는 403
- 화면 렌더링은 `textContent` 만 사용 (XSS 차단)
- 세션 단위 lock(동시 요청 409) + 전역 세마포어(429)
- **`claude -p` 는 서버에서 실제 명령을 실행할 수 있다. 신뢰된 네트워크에만 노출할 것**

## 데이터 초기화

```bash
systemctl stop claude-web
rm -rf data/            # DB + 업로드 전체 삭제
systemctl start claude-web
```

gunicorn 을 쓴다면 세션 lock 이 프로세스 단위이므로 워커는 1개로:

```bash
gunicorn -w 1 --threads 8 -b 0.0.0.0:8080 app:app
```
