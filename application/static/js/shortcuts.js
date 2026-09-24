/* Global Todoist-style keyboard shortcuts.
 *
 *   n        — mark the cursor task as next action / take the mark off
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
      + "tr[data-project-id], .sp-row[data-sp-id], "
      + "li.ps-drop-target[data-project-id]"
    );
    if (row) lastHoveredRow = row;
  });
  document.addEventListener("mouseout", () => {
    if (lastHoveredRow && !lastHoveredRow.isConnected) {
      lastHoveredRow = null;
    }
  });

  // ---- Arrow-key cursor ------------------------------------------------
  // Up/Down move a highlighted "cursor" through the task list (or the
  // project sidebar, depending on where the cursor currently sits). The
  // cursor row is what i / m / d act on, so the whole list is keyboard-
  // drivable without the mouse.
  let cursorRow = null;
  const TASK_SEL = ".task-row[data-task-id], .tree-task[data-task-id]";
  const SIDEBAR_SEL = "#project-sidebar .ps-drop-target";

  function groupOf(row) {
    return row && row.closest("#project-sidebar") ? "sidebar" : "tasks";
  }
  function navRows(group) {
    const sel = group === "sidebar" ? SIDEBAR_SEL : TASK_SEL;
    return Array.from(document.querySelectorAll(sel)).filter(
      (el) => el.offsetParent !== null
    );
  }
  function setCursor(row) {
    if (cursorRow && cursorRow !== row) cursorRow.classList.remove("kbd-cursor");
    cursorRow = row || null;
    if (cursorRow) {
      cursorRow.classList.add("kbd-cursor");
      cursorRow.scrollIntoView({ block: "nearest" });
    }
  }
  function moveCursor(delta) {
    const ref = cursorRow && cursorRow.isConnected ? cursorRow
      : (lastHoveredRow && lastHoveredRow.isConnected ? lastHoveredRow : null);
    const group = ref ? groupOf(ref) : "tasks";
    const rows = navRows(group);
    if (!rows.length) return;
    let idx = ref ? rows.indexOf(ref) : -1;
    if (idx === -1) idx = delta > 0 ? 0 : rows.length - 1;
    else idx = Math.max(0, Math.min(rows.length - 1, idx + delta));
    setCursor(rows[idx]);
  }
  // Hovering takes over from the keyboard cursor so i/d/m act on whatever
  // the user is actually pointing at; arrows then re-establish a cursor.
  document.addEventListener("mouseover", (ev) => {
    const row = ev.target.closest && ev.target.closest(TASK_SEL + ", " + SIDEBAR_SEL);
    if (row && row !== cursorRow) setCursor(null);
  });
  function currentRow() {
    if (cursorRow && cursorRow.isConnected) return cursorRow;
    if (lastHoveredRow && lastHoveredRow.isConnected) return lastHoveredRow;
    return null;
  }
  function openInspectorFor(row) {
    const trigger = row && row.querySelector("[data-inspector-url]");
    if (trigger) { trigger.click(); return true; }
    return false;
  }

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

    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      // Hijack arrows only when there are navigable rows; otherwise let
      // the page scroll as usual.
      const ref = currentRow();
      const group = ref ? groupOf(ref) : "tasks";
      if (navRows(group).length) {
        e.preventDefault();
        moveCursor(e.key === "ArrowDown" ? 1 : -1);
      }
      return;
    }

    if (e.key === "i" || e.key === "I") {
      // Open the inspector for the cursor (or hovered) row.
      const row = currentRow();
      if (row && openInspectorFor(row)) e.preventDefault();
      return;
    }

    if (e.key === "Enter") {
      const row = currentRow();
      if (row && openInspectorFor(row)) e.preventDefault();
      return;
    }

    if (e.key === "m" || e.key === "M") {
      // Move the task at the cursor to a different project via the modal.
      const row = currentRow();
      if (!row) return;
      const tid = row.dataset.taskId;
      if (!tid) return;
      if (typeof window.bktodoOpenMoveTaskModal === "function") {
        e.preventDefault();
        window.bktodoOpenMoveTaskModal(tid, row);
      }
      return;
    }

    if (e.key === "n" || e.key === "N") {
      // Flip the NEXT mark on the row at the cursor. Picking work together is
      // the gesture this whole cursor exists for, and it should cost one key.
      const row = currentRow();
      if (!row) return;
      if (typeof window.bktodoToggleNext === "function"
          && window.bktodoToggleNext(row)) {
        e.preventDefault();
      }
      return;
    }

    if (e.key === "d" || e.key === "D") {
      // Delete the row at the cursor (task / project / folder / section).
      // The shared helper routes to the right endpoint based on which
      // data-* attribute the row carries.
      const row = currentRow();
      if (!row) return;
      if (typeof window.bktodoDeleteRow === "function") {
        e.preventDefault();
        window.bktodoDeleteRow(row);
      }
    }
  });
})();
