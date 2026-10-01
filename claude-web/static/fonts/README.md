# 내장 글꼴

## Pretendard Variable

| | |
|---|---|
| 파일 | `PretendardVariable.woff2` (2,057,688 바이트) |
| 버전 | v1.3.9 |
| 만든 곳 | Kil Hyung-jin — https://github.com/orioncactus/pretendard |
| 라이선스 | SIL Open Font License 1.1 (`OFL.txt`) |
| 받은 곳 | `https://cdn.jsdelivr.net/gh/orioncactus/pretendard@v1.3.9/packages/pretendard/dist/web/variable/woff2/PretendardVariable.woff2` |

가변 글꼴 한 파일로 400에서 600 굵기를 모두 쓴다. 굵기별 파일을 따로 두지 않는다.

## CDN 을 쓰지 않고 직접 담아 두는 이유

이 서비스는 사내망에서 돈다. 서버가 인터넷으로 나가지 못하는 망에 놓일 수 있어서
외부 CDN 링크는 언젠가 반드시 깨진다. 글꼴을 리포지토리에 넣어 두면 망과 무관하게 뜬다.

## 새 버전으로 올릴 때

1. 위 주소의 버전만 바꿔서 `PretendardVariable.woff2` 를 덮어쓴다.
2. `LICENSE` 도 같은 태그에서 다시 받아 `OFL.txt` 로 저장한다.
3. `static/sw.js` 의 `VERSION` 을 올린다. 올리지 않으면 이미 방문한 브라우저가
   예전 글꼴을 계속 쓴다.
4. 이 문서의 파일 크기와 버전을 고친다.

## OFL 이 요구하는 것

- 저작권 표시와 라이선스 원문을 함께 배포한다 → `OFL.txt` 가 그 역할이다.
- 글꼴 파일을 고쳐서 쓸 경우 "Pretendard" 라는 이름을 그대로 쓸 수 없다.
  우리는 원본을 그대로 쓰므로 해당되지 않는다.
