/* The OAuth consent card's only script.
 *
 * It exists because the CSP forbids inline script, and the card still has to
 * do two things the server cannot: pick the theme from the viewer's own
 * machine (stored choice, or Warm Sunset after 18:00 *local*), and make
 * Cancel mean something without inventing a route for it. */
(function () {
  var root = document.documentElement;

  function afterSix() {
    var hour = new Date().getHours();
    return hour >= 18 || hour < 6;
  }

  var stored = "light";
  try { stored = localStorage.getItem("brain.theme") || "light"; } catch (_) {}
  root.dataset.theme = stored === "auto" ? (afterSix() ? "warm_sunset" : "light") : stored;

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
