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
  // Track the row the cursor is currently over — the ``d`` shortcut
  // acts on it. Matches list (.task-row), tree-task (.tree-task),
  // tree-project (.tree-project) and tag-row (.tag-row) variants.
  // Each carries data-task-id, data-project-id or data-tag-name
  // which the delete helper uses to pick the right endpoint.
  let lastHoveredRow = null;
  document.addEventListener("mouseover", (ev) => {
    const row = ev.target.closest && ev.target.closest(
      ".task-row[data-task-id], .tree-task[data-task-id], "
      + ".tree-project[data-project-id], .tag-row[data-tag-name], "
      + "tr[data-project-id], .sp-row[data-sp-id]"
    );
    if (row) lastHoveredRow = row;
  });
  document.addEventListener("mouseout", () => {
    if (lastHoveredRow && !lastHoveredRow.isConnected) {
      lastHoveredRow = null;
    }
  });

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
      return;
    }

    if (e.key === "m" || e.key === "M") {
      // Move the task currently under the cursor to a different
      // project via the modal picker.
      if (!lastHoveredRow || !lastHoveredRow.isConnected) return;
      const tid = lastHoveredRow.dataset.taskId;
      if (!tid) return;
      if (typeof window.bktodoOpenMoveTaskModal === "function") {
        e.preventDefault();
        window.bktodoOpenMoveTaskModal(tid, lastHoveredRow);
      }
      return;
    }

    if (e.key === "d" || e.key === "D") {
      // Delete the row currently under the cursor (task / project /
      // folder / section). The shared helper routes to the right
      // endpoint based on which data-* attribute the row carries.
      if (!lastHoveredRow || !lastHoveredRow.isConnected) return;
      if (typeof window.bktodoDeleteRow === "function") {
        e.preventDefault();
        window.bktodoDeleteRow(lastHoveredRow);
      }
    }
  });
})();
