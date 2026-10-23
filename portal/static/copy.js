// Copy buttons: <button data-copy="ID"> copies the text of the element with that ID. No inline script: the CSP forbids it.
document.addEventListener("click", function (event) {
  var button = event.target.closest("[data-copy]");
  if (!button || !navigator.clipboard) return;
  var source = document.getElementById(button.getAttribute("data-copy"));
  if (source) navigator.clipboard.writeText(source.textContent).then(function () { button.textContent = "Copied"; });
});
