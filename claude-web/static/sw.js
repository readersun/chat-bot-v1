/* claude-web service worker
 *
 * 전략
 *   - /api/*        : 네트워크 전용 (대화 데이터는 절대 캐시하지 않는다)
 *   - 화면 이동     : 네트워크 우선 -> 실패하면 캐시된 앱 셸
 *   - 정적 리소스   : 캐시 우선 + 백그라운드 갱신
 */
const VERSION = "claude-web-v1";
const SHELL = [
  "/",
  "/manifest.webmanifest",
  "/static/icons/icon-192.png",
  "/static/icons/icon-512.png",
  "/static/icons/apple-touch-icon.png",
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(VERSION)
      .then((cache) => cache.addAll(SHELL))
      .catch(() => {})
      .then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(
        keys.filter((k) => k !== VERSION).map((k) => caches.delete(k))
      ))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  const req = event.request;
  if (req.method !== "GET") { return; }

  const url = new URL(req.url);
  if (url.origin !== self.location.origin) { return; }

  // 대화/프로젝트 데이터와 첨부 이미지는 항상 서버에서 가져온다
  if (url.pathname.startsWith("/api/") || url.pathname === "/health") {
    return;
  }

  // 페이지 이동: 네트워크 우선, 오프라인이면 캐시된 셸
  if (req.mode === "navigate") {
    event.respondWith(
      fetch(req)
        .then((res) => {
          const copy = res.clone();
          caches.open(VERSION).then((c) => c.put("/", copy)).catch(() => {});
          return res;
        })
        .catch(() => caches.match("/").then((hit) => hit || offlinePage()))
    );
    return;
  }

  // 정적 리소스: 캐시 우선
  event.respondWith(
    caches.match(req).then((hit) => {
      const network = fetch(req)
        .then((res) => {
          if (res && res.status === 200) {
            const copy = res.clone();
            caches.open(VERSION).then((c) => c.put(req, copy)).catch(() => {});
          }
          return res;
        })
        .catch(() => hit);
      return hit || network;
    })
  );
});

function offlinePage() {
  return new Response(
    "<!doctype html><meta charset='utf-8'>" +
    "<meta name='viewport' content='width=device-width,initial-scale=1'>" +
    "<style>body{font-family:system-ui,-apple-system,'Segoe UI','Noto Sans KR',sans-serif;" +
    "background:#17181c;color:#e8e8ea;display:grid;place-items:center;height:100dvh;margin:0;" +
    "text-align:center;padding:24px}</style>" +
    "<div><h2>오프라인</h2><p>서버에 연결할 수 없습니다.<br>네트워크를 확인한 뒤 새로고침해 주세요.</p></div>",
    { headers: { "Content-Type": "text/html; charset=utf-8" }, status: 503 }
  );
}
