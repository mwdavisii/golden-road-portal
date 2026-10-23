// The register page's draft (spec P20). An agent puts a registration-draft/v1 document, base64url JSON, in the URL fragment.
// The fragment never reaches the server. This script checks its shape and size and fills the form fields as text (.value,
// .textContent), never as HTML. Signed out, it keeps the raw fragment in this tab's sessionStorage until the person is back
// from signing in, then forgets it. The server checks everything again on submit.
(function () {
  "use strict";
  var MAX = 16384;
  var KEY = "golden-road-register-draft";
  var FIELDS = { schema: true, repo: true, purpose: true, data_classification: true, proposed_tier: true };
  var CLASSIFICATIONS = ["public", "internal", "restricted", "phi"];
  var TIERS = ["low", "moderate", "high"];
  var REPO = /^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/;

  function fail(error) { return { ok: false, error: error }; }

  function decode(fragment) {
    var b64 = fragment.replace(/-/g, "+").replace(/_/g, "/");
    while (b64.length % 4) b64 += "=";
    var bytes = Uint8Array.from(atob(b64), function (c) { return c.charCodeAt(0); });
    return new TextDecoder("utf-8", { fatal: true }).decode(bytes);
  }

  function parseDraft(fragment) {
    if (typeof fragment !== "string" || fragment.length === 0) return fail("empty");
    if (fragment.length > MAX) return fail("too_large");
    if (!/^[A-Za-z0-9_-]+$/.test(fragment)) return fail("not_base64url");
    var text, doc;
    try { text = decode(fragment); } catch (e) { return fail("not_base64url"); }
    try { doc = JSON.parse(text); } catch (e) { return fail("not_json"); }
    if (doc === null || typeof doc !== "object" || Array.isArray(doc)) return fail("not_object");
    var keys = Object.keys(doc);
    for (var i = 0; i < keys.length; i++) if (!Object.prototype.hasOwnProperty.call(FIELDS, keys[i])) return fail("unknown_field");
    if (doc.schema !== "registration-draft/v1") return fail("schema");
    if (typeof doc.repo !== "string" || !REPO.test(doc.repo)) return fail("repo");
    if ("purpose" in doc && (typeof doc.purpose !== "string" || Array.from(doc.purpose).length > 4000  /* code points, as Python counts them */)) return fail("purpose");
    if ("data_classification" in doc && CLASSIFICATIONS.indexOf(doc.data_classification) < 0) return fail("data_classification");
    if ("proposed_tier" in doc && TIERS.indexOf(doc.proposed_tier) < 0) return fail("proposed_tier");
    return { ok: true, draft: { repo: doc.repo, purpose: doc.purpose || "", data_classification: doc.data_classification || "",
                                proposed_tier: doc.proposed_tier || "" } };
  }

  function choose(select, value) {
    for (var i = 0; i < select.options.length; i++) {
      if (select.options[i].value === value) { select.selectedIndex = i; return true; }
    }
    return false;
  }

  function start() {
    var raw = location.hash.slice(1);
    var store = window.sessionStorage;
    if (raw) {
      try { store.setItem(KEY, raw); } catch (e) { /* storage off: the draft works only while signed in */ }
      history.replaceState(null, "", location.pathname);
    } else {
      try { raw = store.getItem(KEY) || ""; } catch (e) { raw = ""; }
    }
    var form = document.getElementById("register-form");
    var status = document.getElementById("draft-status");
    if (!raw || !form) return;  // signed out: keep the draft for after sign-in
    try { store.removeItem(KEY); } catch (e) { /* nothing kept */ }
    var result = parseDraft(raw);
    if (!result.ok) { status.textContent = "The draft in the link was not used (" + result.error + "). Fill in the form yourself."; return; }
    var d = result.draft;
    var repoKnown = choose(form.elements.repo, d.repo);
    form.elements.purpose.value = d.purpose;
    if (d.data_classification) choose(form.elements.data_classification, d.data_classification);
    if (d.proposed_tier) choose(form.elements.proposed_tier, d.proposed_tier);
    status.textContent = repoKnown ? "Filled from a draft. Check every field before you submit."
      : "Filled from a draft, but " + d.repo + " is not one of your repositories. Check every field before you submit.";
  }

  if (typeof document !== "undefined") document.addEventListener("DOMContentLoaded", start);
  if (typeof module !== "undefined") module.exports = { parseDraft: parseDraft, MAX: MAX };
})();
