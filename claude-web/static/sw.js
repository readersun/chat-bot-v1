/* chat-bot service worker
 *
 * 로그인이 도입되면서 캐시 정책이 바뀌었다.
 *
 *   - 앱 셸("/")은 더 이상 캐시하지 않는다.
 *     로그인 후의 "/" 응답에는 로그인한 사용자 이름과 CSRF 토큰이 들어간다.
 *     이를 캐시하면 같은 기기를 쓰는 다른 사람에게 노출될 수 있다.
 *   - /api/*, /admin/*, /notes, /patch, /login, /logout, /setup 도 캐시하지 않는다.
 *   - /patch 와 /_patch/ 는 특히 중요하다. 패치 tar 는 GB 단위라 캐시에
 *     들어가면 사용자 노트북 디스크가 찬다.
 *     대화 내용, 첨부 이미지, 관리자 설정, 개인 메모가 디스크에 남지 않아야 한다.
 *     메모 API(/api/notes)와 메모 첨부(/api/note-attachments)는 /api/ 규칙에
 *     이미 걸리지만, 의도를 분명히 하려고 /notes 를 목록에 함께 적어 둔다.
 *   - 캐시 대상은 로그인과 무관한 정적 리소스뿐이다.
 *     아이콘, CSS, manifest, 그리고 내장 글꼴(Pretendard)이다.
 *
 * 그래서 오프라인일 때는 안내 페이지만 뜬다. 인증이 필요한 앱에서는
 * 이것이 올바른 절충이다.
 *
 * VERSION 을 올리면 activate 에서 이전 캐시를 통째로 지운다.
 * v1 이 캐시해 둔 "/" 응답도 이때 함께 제거된다.
 */
const VERSION = "chat-bot-v9";

const SHELL = [
  "/manifest.webmanifest",
  "/static/type.css",
  "/static/shared.css",
  "/static/rail.css",
  "/static/rail.js",
  /* 내장 글꼴. 로그인과 무관한 정적 파일이라 캐시해도 된다.
     여기서 빼면 오프라인일 때 글꼴만 빠져서 화면이 달라 보인다. */
  "/static/fonts/PretendardVariable.woff2",
  "/static/icons/icon-192.png",
  "/static/icons/icon-512.png",
  "/static/icons/apple-touch-icon.png",
];

// 어떤 경우에도 캐시하지 않을 경로
const NEVER_CACHE = ["/api/", "/admin", "/notes", "/patch", "/_patch/",
                     "/no-access", "/login", "/logout", "/setup",
                     "/health", "/sw.js"];

function isPrivate(pathname) {
  return NEVER_CACHE.some((p) => pathname === p || pathname.startsWith(p));
}

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

  // 인증/대화 관련 경로는 서비스워커가 관여하지 않는다
  if (isPrivate(url.pathname)) { return; }

  // 페이지 이동: 항상 네트워크. 응답을 저장하지 않는다.
  // (로그인 리다이렉트와 사용자별 내용이 캐시에 남지 않게 한다)
  if (req.mode === "navigate") {
    event.respondWith(fetch(req).catch(() => offlinePage()));
    return;
  }

  // 정적 리소스: 캐시 우선 + 백그라운드 갱신
  event.respondWith(
    caches.match(req).then((hit) => {
      const network = fetch(req)
        .then((res) => {
          if (res && res.status === 200 && res.type === "basic") {
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
    /* 이 페이지는 네트워크가 끊긴 상태에서 뜬다. 바깥 CSS 를 기다릴 수 없으니
       색과 글꼴을 여기에 직접 적는다. 값은 shared.css 의 토큰과 같다. */
    "<style>body{font-family:'Pretendard Variable',Pretendard,system-ui,-apple-system," +
    "'Segoe UI','Apple SD Gothic Neo','Malgun Gothic',sans-serif;" +
    "background:#eff3f6;color:#1f2933;display:grid;place-items:center;height:100dvh;margin:0;" +
    "text-align:center;padding:24px;line-height:1.75}" +
    "h2{font-weight:600;letter-spacing:-.005em;margin:0 0 8px}" +
    "p{margin:0;color:#5a6772;font-size:14px}" +
    "@media(prefers-color-scheme:dark){body{background:#14181c;color:#e7ecf0}" +
    "p{color:#a3afb9}}</style>" +
    "<div><h2>오프라인</h2><p>서버에 연결할 수 없습니다.<br>네트워크를 확인한 뒤 새로고침해 주세요.</p></div>",
    { headers: { "Content-Type": "text/html; charset=utf-8" }, status: 503 }
  );
}
