// The register page's draft (spec P20). An agent puts a registration-draft/v1 document, base64url JSON, in the URL fragment.
// The fragment never reaches the server. This script checks its shape and size and fills the form fields as text (.value,
// .textContent), never as HTML. Signed out, it keeps the raw fragment in this tab's sessionStorage until the person is back
// from signing in, then forgets it. The server checks everything again on submit.
(function () {
  "use strict";
  var MAX = 16384;
  var KEY = "golden-road-register-draft";
  // proposed_tier is retired in 0.5.0: an old link may carry it; accepted, never used
  var FIELDS = { schema: true, repo: true, purpose: true, data_classification: true, proposed_tier: true };
  var CLASSIFICATIONS = ["public", "confidential", "protected"];
  var RETIRED = { internal: "confidential", restricted: "confidential", phi: "protected" };  // builder 0.5.0: no alias, a note
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
    var notes = [], classification = doc.data_classification || "";
    if (typeof classification === "string" && Object.prototype.hasOwnProperty.call(RETIRED, classification)) {
      notes.push("`" + classification + "` is retired: use `" + RETIRED[classification] + "`. Choose the data classification yourself.");
      classification = "";
    } else if ("data_classification" in doc && CLASSIFICATIONS.indexOf(doc.data_classification) < 0) return fail("data_classification");
    return { ok: true, notes: notes, draft: { repo: doc.repo, purpose: doc.purpose || "", data_classification: classification } };
  }

  function choose(select, value) {
    for (var i = 0; i < select.options.length; i++) {
      if (select.options[i].value === value) { select.selectedIndex = i; return true; }
    }
    return false;
  }

  function selected(select) {
    return select.selectedIndex >= 0 ? select.options[select.selectedIndex].value : "";
  }

  // The change page's "current registration" card is about one repo (data-repo). When another repository is chosen, or a draft names
  // one, the card is hidden, and what the page pre-filled from it (data-prefilled) and the person hasn't edited is cleared, once, so
  // another repo's purpose is never submitted by mistake. Returns whether the card is about the chosen repo.
  function syncCard(form, card, status) {
    if (!card) return false;
    var repo = card.getAttribute("data-repo") || "";
    var same = selected(form.elements.repo).toLowerCase() === repo.toLowerCase();
    card.hidden = !same;
    if (!same && card.getAttribute("data-prefilled") === "true") {
      card.setAttribute("data-prefilled", "false");
      var purpose = form.elements.purpose, radios = form.elements.data_classification;
      if (purpose.value === purpose.defaultValue) {
        purpose.value = "";
        purpose.dispatchEvent(new Event("input", { bubbles: true }));
      }
      var untouched = true;
      for (var i = 0; radios && i < radios.length; i++) if (radios[i].checked !== radios[i].defaultChecked) untouched = false;
      for (var j = 0; untouched && radios && j < radios.length; j++) radios[j].checked = false;
      status.hidden = false;
      status.textContent = "The form was filled from the current registration of " + repo + ": what you hadn't changed was cleared. "
        + "Write this repository's own purpose and classification.";
    }
    return same;
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
    var card = document.getElementById("current-card");
    if (form && card) form.elements.repo.addEventListener("change", function () { syncCard(form, card, status); });
    if (!raw || !form) return;  // signed out: keep the draft for after sign-in
    try { store.removeItem(KEY); } catch (e) { /* nothing kept */ }
    fill(form, status, parseDraft(raw), card);
  }

  // Fill the form from a parsed draft, and say what happened in the status line.
  function fill(form, status, result, card) {
    status.hidden = false;
    if (!result.ok) { status.textContent = "The draft in the link was not used (" + result.error + "). Fill in the form yourself."; return; }
    var d = result.draft;
    var repoKnown = choose(form.elements.repo, d.repo);
    var replacing = card && card.getAttribute("data-prefilled") === "true" && syncCard(form, card, status);
    form.elements.purpose.value = d.purpose;
    form.elements.purpose.dispatchEvent(new Event("input", { bubbles: true }));  // so the purpose's counter (forms.js) updates
    if (d.data_classification) form.elements.data_classification.value = d.data_classification;  // a radio group
    var note = result.notes.length ? " " + result.notes.join(" ") : "";
    if (card && !replacing) syncCard(form, card, status);  // a draft for another repo: the card isn't about it
    status.textContent = (repoKnown ? "Filled from a draft" + (replacing ? ": it replaced the text of the current registration" : "")
      + ". Check every field before you submit."
      : "Filled from a draft, but " + d.repo + " is not one of your repositories. Check every field before you submit.") + note;
  }

  if (typeof document !== "undefined") document.addEventListener("DOMContentLoaded", start);
  if (typeof module !== "undefined") module.exports = { parseDraft: parseDraft, fill: fill, syncCard: syncCard, MAX: MAX };
})();
