// The service principal form's button (spec 2026-11-16 S3). No inline script: the CSP forbids it. Without JavaScript the button says
// "Create or request" and the server decides; this only says sooner which it will be. It writes only .textContent and .hidden.
(function () {
  "use strict";

  function buttonLabel(requestable) { return requestable ? "Request" : "Create"; }

  function wire(form) {
    var button = form.querySelector("[data-sp-submit]"), why = form.querySelector("[data-sp-justification]");
    if (!button) return false;
    function update() {
      var ticked = form.querySelectorAll('input[data-tier="requestable"]:checked').length > 0;
      button.textContent = buttonLabel(ticked);
      if (why) why.hidden = !ticked;
    }
    form.addEventListener("change", update);
    update();
    return true;
  }

  function start() {
    Array.prototype.forEach.call(document.querySelectorAll("form[data-service-principal]"), function (form) {
      try { wire(form); } catch (e) { /* the form still works as the server rendered it */ }
    });
  }

  if (typeof document !== "undefined") document.addEventListener("DOMContentLoaded", start);
  if (typeof module !== "undefined") module.exports = { buttonLabel: buttonLabel };
})();
