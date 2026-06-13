// Seller-Market mgmt UI - tiny client helpers.
// HTMX 2.x handles HX-Redirect natively, so no extra wiring is needed here.

function confirmRiskyAction(message) {
  return window.confirm(message || "Are you sure? This action cannot be undone.");
}

// Expose for inline onclick handlers on destructive buttons.
window.confirmRiskyAction = confirmRiskyAction;

// CSRF (Phase 10): attach the token from <meta name="csrf-token"> to every
// HTMX request as the X-CSRF-Token header. The server middleware in
// ``app.security.csrf`` compares this against the csrf_token cookie value
// using the double-submit pattern. Plain HTML <form method="post"> posts
// use the hidden ``csrf_token`` input from ``templates/partials/csrf.html``
// instead; this hook only covers the HTMX path.
document.addEventListener("htmx:configRequest", function (e) {
  var meta = document.querySelector('meta[name="csrf-token"]');
  var token = meta ? meta.content : "";
  if (token) {
    e.detail.headers["X-CSRF-Token"] = token;
  }
});

// Mobile tabs (responsive nav): center the active tab inside the horizontally
// scrollable .tabs strip. Only acts when the strip is an actual scroll
// container (the <=800px layout, where overflow-x:auto is set) and only ever
// scrolls the strip itself — never scrollIntoView, which would scroll ancestor
// scrollers including the document and shift the whole desktop page on load.
// Fully wrapped so this nav nicety can never break the page.
(function () {
  function revealActiveTab() {
    try {
      var tab = document.querySelector(".tabs .tab--active");
      if (!tab || typeof tab.closest !== "function") return;
      var strip = tab.closest(".tabs");
      if (!strip) return;
      var overflowX = window.getComputedStyle(strip).overflowX;
      if (overflowX !== "auto" && overflowX !== "scroll") return; // desktop: not a scroller
      if (strip.scrollWidth <= strip.clientWidth) return; // nothing to scroll
      var tabRect = tab.getBoundingClientRect();
      var stripRect = strip.getBoundingClientRect();
      // Shift the strip's own scroll so the tab sits centered; the browser
      // clamps scrollLeft to the valid range. The document scroll is untouched.
      strip.scrollLeft +=
        tabRect.left - stripRect.left - (strip.clientWidth - tabRect.width) / 2;
    } catch (e) {
      /* a nav nicety must never break the page */
    }
  }
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", revealActiveTab);
  } else {
    revealActiveTab();
  }
})();

