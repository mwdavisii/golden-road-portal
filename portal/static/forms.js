// Form helpers (spec 2026-10-30 R3 and R8). No inline script: the CSP forbids it. Without JavaScript every form still works and the
// server checks everything; this only shows the counts and says early why a button would be refused. It writes only .textContent,
// .hidden and .disabled, never HTML.
(function () {
  "use strict";

  function length(text) { return Array.from(text || "").length; }  // code points, as Python counts them

  // Spaces and tabs only: a newline must stay, so the one-line rule below sees it. The server decides; this is only an early hint.
  function trimLine(text) { return (text || "").replace(/^[ \t]+|[ \t]+$/g, ""); }

  // "12 / 4000"; while a minimum isn't met, "12 / 30 minimum".
  function countLabel(text, min, max) {
    var n = length(text);
    return min && n < min ? n + " / " + min + " minimum" : n + " / " + max;
  }

  // Which of the tier and the classification are below the floor (as portal/floor.py below_floor). Nothing chosen is nothing below.
  function below(tiers, classes, tier, cls, floorTier, floorCls) {
    var out = [];
    if (tier && tiers.indexOf(tier) < tiers.indexOf(floorTier)) out.push("tier");
    if (cls && classes.indexOf(cls) < classes.indexOf(floorCls)) out.push("data classification");
    return out;
  }

  // Why Approve would be refused, or "" when the form looks valid. The words are the server's (portal/registration.py).
  function approveProblem(f) {
    var just = trimLine(f.justification), reason = trimLine(f.override);
    if (!f.tier) return "Choose a tier.";
    if (!f.classification) return "Choose a data classification.";
    if (/[\r\n]/.test(just) || length(just) < 10 || length(just) > 1000) return "Give a justification of 10 to 1000 characters.";
    if (f.below.length && (/[\r\n]/.test(reason) || length(reason) < 30 || length(reason) > 1000))
      return "The " + f.below.join(" and ") + " is below the floor: give a reason of 30 to 1000 characters.";
    return "";
  }

  function checkedValue(form, name) {
    var el = form.querySelector('input[name="' + name + '"]:checked');
    return el ? el.value : "";
  }

  function wireCounters(root) {
    Array.prototype.forEach.call(root.querySelectorAll("[data-counter]"), function (field) {
      var out = document.getElementById(field.getAttribute("data-counter"));
      if (!out) return;
      var min = parseInt(field.getAttribute("data-min") || "0", 10), max = parseInt(field.getAttribute("maxlength") || "0", 10);
      function update() { out.textContent = countLabel(field.value, min, max); }
      field.addEventListener("input", update);
      update();
    });
  }

  // Wire one approval form; a form missing any part it needs is left as the server rendered it (false).
  function wireApproval(form) {
    var tierList = form.getAttribute("data-tiers"), classList = form.getAttribute("data-classifications");
    var floorTier = form.getAttribute("data-floor-tier"), floorCls = form.getAttribute("data-floor-classification");
    var button = form.querySelector("[data-approve]"), why = form.querySelector("[data-approve-why]");
    var override = form.querySelector("[data-override]");
    var fields = form.elements || {};
    if (!tierList || !classList || !floorTier || !floorCls || !button || !why || !fields.justification || !fields.floor_override_reason)
      return false;
    var tiers = tierList.split(" "), classes = classList.split(" ");
    function update() {
      var tier = checkedValue(form, "tier"), cls = checkedValue(form, "data_classification");
      var low = below(tiers, classes, tier, cls, floorTier, floorCls);
      if (override) override.hidden = low.length === 0;
      var problem = approveProblem({ tier: tier, classification: cls, justification: form.elements.justification.value,
                                     override: form.elements.floor_override_reason.value, below: low });
      button.disabled = problem !== "";
      why.textContent = problem;
    }
    form.addEventListener("input", update);
    form.addEventListener("change", update);
    update();
    return true;
  }

  // Each form on its own: one that throws can't leave the others unwired. Returns how many threw.
  function wireForms(forms) {
    var errors = 0;
    Array.prototype.forEach.call(forms, function (form) {
      try { wireApproval(form); } catch (e) { errors += 1; }
    });
    return errors;
  }

  function start() {
    wireCounters(document);
    wireForms(document.querySelectorAll("form[data-approval]"));
  }

  if (typeof document !== "undefined") document.addEventListener("DOMContentLoaded", start);
  if (typeof module !== "undefined") module.exports = { countLabel: countLabel, below: below, approveProblem: approveProblem, wireForms: wireForms };
})();
