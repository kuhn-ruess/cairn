/* Global Todoist-style keyboard shortcuts.
 *
 *   q        — quick add (wired in quick-add.js)
 *   p        — command bar / quick jump (wired in quickjump.js)
 *   g then i — Inbox
 *   g then t — Today
 *   g then u — Forecast (Todoist calls this Upcoming)
 *   g then n — Next
 *   g then f — Flagged
 *   g then w — Waiting
 *   g then s — Someday
 *   g then p — Projects
 *   g then r — Review
 *   /        — focus the navbar search field
 *   ?        — show shortcut help
 */
(function () {
  const ROUTES = {
    i: "/tasks/inbox",
    t: "/tasks/today",
    u: "/tasks/forecast",
    n: "/tasks/next",
    f: "/tasks/flagged",
    w: "/tasks/waiting",
    s: "/tasks/someday",
    p: "/projects/",
    r: "/review/",
    h: "/",
  };

  function shouldIgnore(e) {
    const t = e.target;
    if (!t) return false;
    const tag = (t.tagName || "").toUpperCase();
    if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") return true;
    if (t.isContentEditable) return true;
    if (e.metaKey || e.ctrlKey || e.altKey) return true;
    return false;
  }

  let gPrimed = false;
  let gTimer = null;
  function primeG() {
    gPrimed = true;
    clearTimeout(gTimer);
    gTimer = setTimeout(() => {
      gPrimed = false;
    }, 1200);
  }

  document.addEventListener("keydown", (e) => {
    if (shouldIgnore(e)) return;

    if (gPrimed) {
      gPrimed = false;
      clearTimeout(gTimer);
      const dest = ROUTES[e.key.toLowerCase()];
      if (dest) {
        e.preventDefault();
        window.location.assign(dest);
      }
      return;
    }

    if (e.key === "g") {
      e.preventDefault();
      primeG();
      return;
    }

    if (e.key === "/") {
      const search = document.querySelector('input[type="search"][name="q"]');
      if (search) {
        e.preventDefault();
        search.focus();
        search.select();
      }
      return;
    }

    if (e.key === "?") {
      e.preventDefault();
      const m = document.getElementById("shortcutsModal");
      if (m && window.bootstrap) {
        bootstrap.Modal.getOrCreateInstance(m).show();
      }
    }
  });
})();
