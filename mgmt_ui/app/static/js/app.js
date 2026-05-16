// Seller-Market mgmt UI - tiny client helpers.
// HTMX 2.x handles HX-Redirect natively, so no extra wiring is needed here.

function confirmRiskyAction(message) {
  return window.confirm(message || "Are you sure? This action cannot be undone.");
}

// Expose for inline onclick handlers on destructive buttons.
window.confirmRiskyAction = confirmRiskyAction;
