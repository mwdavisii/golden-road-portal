// Start here (spec 2026-10-30 R2): the prerequisites as a checklist. The ticks are a convenience in this tab only (sessionStorage);
// nothing is sent anywhere, and without JavaScript the list still reads as steps. Writes only .checked, .value, .max, .hidden and
// .textContent, never HTML.
(function () {
  "use strict";
  var KEY = "golden-road-start";

  // The saved ticks: only known ids, each once. Anything else (not JSON, not a list) is no ticks.
  function parseSaved(text, known) {
    var list;
    try { list = JSON.parse(text || "[]"); } catch (e) { return []; }
    if (!Array.isArray(list)) return [];
    return list.filter(function (id, i) { return typeof id === "string" && known.indexOf(id) >= 0 && list.indexOf(id) === i; });
  }

  function progressLabel(done, total) { return done + " of " + total + " done"; }

  function start() {
    var boxes = Array.prototype.slice.call(document.querySelectorAll("input[data-prereq]"));
    var bar = document.getElementById("start-progress"), label = document.getElementById("start-progress-label");
    if (!boxes.length || !bar || !label) return;
    var ids = boxes.map(function (b) { return b.id; }), saved = [];
    try { saved = parseSaved(window.sessionStorage.getItem(KEY), ids); } catch (e) { saved = []; }
    boxes.forEach(function (b) { b.checked = saved.indexOf(b.id) >= 0; });
    function update() {
      var done = boxes.filter(function (b) { return b.checked; }).map(function (b) { return b.id; });
      bar.max = boxes.length;
      bar.value = done.length;
      label.textContent = progressLabel(done.length, boxes.length);
      try { window.sessionStorage.setItem(KEY, JSON.stringify(done)); } catch (e) { /* storage off: the ticks last until the page closes */ }
    }
    boxes.forEach(function (b) { b.addEventListener("change", update); });
    bar.parentNode.hidden = false;
    update();
  }

  if (typeof document !== "undefined") document.addEventListener("DOMContentLoaded", start);
  if (typeof module !== "undefined") module.exports = { parseSaved: parseSaved, progressLabel: progressLabel };
})();
