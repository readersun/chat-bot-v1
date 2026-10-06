# -*- coding: utf-8 -*-
"""
gunicorn 운영 설정.

    /opt/claude-web/venv/bin/gunicorn -c deploy/gunicorn.conf.py app:app

워커 수를 왜 1 로 두는가 (중요)
------------------------------
이 앱은 다음 세 가지를 **프로세스 메모리** 에 둔다.

    app.ConcurrencyLimiter   전체 동시 Claude 실행 수 제한 (max_concurrent_claude)
    app._SESSION_LOCKS       같은 세션 동시 요청 차단 (public 세션 문맥 보호)
    auth._setup_token        최초 관리자 bootstrap 토큰

gunicorn 워커는 서로 다른 프로세스라서 위 상태가 공유되지 않는다.
워커를 N 개로 늘리면

    - 동시 실행 제한이 사실상 N 배가 된다 (3 으로 설정해도 실제로는 3*N)
    - 같은 public 세션에 두 사람이 동시에 보내면 서로 다른 워커에 걸려
      세션 lock 을 통과해 버리고, claude --resume 문맥이 섞인다
    - /setup 의 bootstrap 토큰이 워커마다 달라 최초 관리자 생성이 간헐적으로 실패한다

그래서 **워커는 1 개, 동시성은 스레드로** 낸다. 개발 서버(app.run(threaded=True))와
동작이 정확히 같아진다. 로그인 rate limit 과 설정값은 SQLite 에 있으므로 영향 없다.

사용자가 수십 명 수준인 사내 도구에서는 이 구성으로 충분하다.
그 이상으로 키워야 하면 워커를 늘리기 전에 위 세 가지를 DB/파일 lock 으로
옮겨야 한다. 워커만 늘리면 조용히 깨진다.
"""

import multiprocessing  # noqa: F401  (아래 주석의 계산식 참고용)
import os


def _int(name, default):
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


# --- 바인딩 ----------------------------------------------------------------
# 외부에 직접 노출하지 않는다. nginx 만 여기로 붙는다.
bind = os.getenv("GUNICORN_BIND", "127.0.0.1:8080")

# --- 워커 ------------------------------------------------------------------
workers = 1                       # 위 설명대로 반드시 1
worker_class = "gthread"
# 스레드는 32개. PuTTY 터널은 아래로 흐르는 스트림 두 개가 스레드를 하나씩
# 쥐고(클라이언트 쪽, 중계 쪽), 사람마다 있는 중계의 롱폴도 25초씩 하나를 쥔다.
# 8개로는 중계 몇 대와 터널 하나면 채팅이 밀린다. 터널을 열기 전에 채팅 몫
# (TUNNEL_RESERVED_THREADS, 기본 12)을 남겨 두고 계산한다 — tunnel_store.capacity_left.
# 이 값은 config.GUNICORN_THREADS 가 같은 환경변수로 읽는다. 둘이 같아야 한다.
threads = _int("GUNICORN_THREADS", 32)

# --- 타임아웃 --------------------------------------------------------------
# 반드시  Claude timeout(관리자 > Claude 설정) < gunicorn timeout < nginx proxy_read_timeout
# 기본값 기준:   180초        <        300초        <        360초
timeout = _int("GUNICORN_TIMEOUT", 300)
graceful_timeout = 30
keepalive = 5

# --- 로그 ------------------------------------------------------------------
# systemd 가 stdout/stderr 를 journal 로 가져간다. 별도 로그 파일을 두지 않는다.
accesslog = "-"
errorlog = "-"
loglevel = os.getenv("GUNICORN_LOGLEVEL", "info")
# 기본 포맷의 마지막 "%(f)s" 는 Referer 다. 사내 도구라 굳이 남기지 않는다.
access_log_format = '%(h)s "%(r)s" %(s)s %(b)s %(L)ss "%(a)s"'

# --- 기타 ------------------------------------------------------------------
proc_name = "claude-web"
# 소스 디렉터리를 읽기 전용으로 마운트하므로 .pyc 를 만들지 않는다.
os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")
