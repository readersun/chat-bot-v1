#!/usr/bin/env bash
#
# Docker 배포용 업데이트
#
#   ./scripts/docker-update.sh            소스만 갱신 (git pull + restart)
#   ./scripts/docker-update.sh --build    의존성/런타임까지 갱신 (이미지 재빌드)
#
# 어느 쪽이든 git pull 전에 DB 를 백업한다. 마이그레이션은 app.py 를 import 할 때
# 자동으로 돌지만(transaction + 자동 백업), 사람이 만든 백업을 하나 더 남긴다.
#
set -euo pipefail

cd "$(dirname "$0")/.."            # = compose 프로젝트 디렉터리 (claude-web/)

BUILD=0
[ "${1:-}" = "--build" ] && BUILD=1

echo "== 1/5 백업 =="
./scripts/docker-backup.sh >/dev/null || {
    echo "!! 백업 실패. 업데이트를 중단합니다." >&2; exit 1; }
echo "   완료"

echo "== 2/5 git pull =="
git -C .. pull --ff-only
echo "   HEAD = $(git -C .. rev-parse --short HEAD)"

if [ "$BUILD" = "1" ]; then
    echo "== 3/5 이미지 재빌드 =="
    docker compose build app
    echo "== 4/5 재생성 =="
    docker compose up -d
else
    echo "== 3/5 이미지 재빌드: 생략 (--build 를 주면 수행) =="
    echo "== 4/5 app 재시작 =="
    # 소스는 bind mount 라서 이미 컨테이너에 보이지만, gunicorn 워커가 파이썬
    # 모듈과 Jinja 템플릿을 메모리에 들고 있으므로 재시작해야 반영된다.
    docker compose restart app
fi

echo "== 5/5 확인 =="
docker compose ps
sleep 3
docker compose logs --tail=40 app

PORT="$(grep -E '^HTTP_PORT=' .env 2>/dev/null | tail -1 | cut -d= -f2- || true)"
PORT="${PORT:-80}"
echo
echo "-- health --"
curl -fsS "http://127.0.0.1:${PORT}/health" && echo
echo
echo "마이그레이션이 적용됐는지 로그에서 'DB 마이그레이션' / '[migrate]' 줄을 확인하세요."
