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

