/* 좁은 화면에서 왼쪽 레일을 서랍으로 여닫는다.
 *
 * 넓은 화면(900px 이상)에서는 레일이 상시로 보이므로 이 스크립트가 하는 일이
 * 눈에 드러나지 않는다. 여는 단추 자체가 CSS 로 숨겨져 있다.
 *
 * 채팅 화면에는 세션 판을 여닫는 별도의 서랍(#nav / body.nav-open)이 있다.
 * 겹치지 않도록 이쪽은 body.rail-open 과 자기 몫의 덮개만 쓴다.
 */
(function () {
  "use strict";

  var rail = document.querySelector(".rail");
  var btn = document.getElementById("rail-toggle");
  if (!rail || !btn) { return; }

  var backdrop = document.createElement("div");
  backdrop.className = "rail-backdrop";
  document.body.appendChild(backdrop);

  function close() {
    document.body.classList.remove("rail-open");
    btn.setAttribute("aria-expanded", "false");
  }

  function open() {
    document.body.classList.add("rail-open");
    btn.setAttribute("aria-expanded", "true");
  }

  btn.addEventListener("click", function (e) {
    e.stopPropagation();
    if (document.body.classList.contains("rail-open")) { close(); } else { open(); }
  });

  backdrop.addEventListener("click", close);

  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape") { close(); }
  });

  /* 다른 화면으로 넘어가는 링크를 눌렀을 때. 넘어가는 동안 서랍이 열린 채로
     남아 있으면 화면이 깜빡이는 것처럼 보인다. */
  rail.addEventListener("click", function (e) {
    if (e.target.closest("a")) { close(); }
  });
}());
