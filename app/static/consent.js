/* The OAuth consent card's only script.
 *
 * It exists because the CSP forbids inline script, and the card still has to
 * do two things the server cannot: pick the theme from the viewer's own
 * machine (stored choice, or Warm Sunset after 18:00 *local*), and make
 * Cancel mean something without inventing a route for it. */
(function () {
  var root = document.documentElement;

  // `R-10`: two themes, light and dark. The Warm Sunset third and its 18:00
  // switch were dropped from the design, so "auto" follows the machine rather
  // than the clock — which is what a person who picked auto meant anyway.
  var stored = "light";
  try { stored = localStorage.getItem("brain.theme") || "light"; } catch (_) {}
  if (stored === "auto") {
    var dark = window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches;
    stored = dark ? "dark" : "light";
  }
  root.dataset.theme = stored === "dark" ? "dark" : "light";

  var cancel = document.getElementById("consent-cancel");
  if (cancel) {
    cancel.addEventListener("click", function () {
      // Nothing is sent: no code is issued and no session is created. Going
      // back is honest — the client's redirect never fires, which is exactly
      // what "I did not authorize this" should look like to it.
      if (window.history.length > 1) window.history.back();
      else window.location.href = "/";
    });
  }
})();
