/* One-click NEXT on a task row — the arrow toggles the mark, the caret menu
 * marks it next AND sets a duration in the same click.
 *
 * Why it exists: picking work together meant opening the inspector and finding
 * the right entry in a nine-value status select. The decision is instant; the
 * scrolling was the cost. `n` on the row under the cursor (shortcuts.js) is the
 * same gesture without the mouse.
 *
 * Delegated on `document` throughout, and it resolves its buttons at fire time:
 * every list in this app re-renders by swapping `<main>`'s innerHTML
 * (`bktodo:refresh-needed` → `bktodoRefreshMain`), which throws away anything
 * bound to the rows themselves. See the "main-swap kills inline JS" note.
 *
 * The page is rebuilt from the server after a successful call rather than
 * patched here: the mark decides whether a task shows up in Next, in Today's
 * "Next actions" rail and in the plan backlog, and those are computed
 * server-side. One partial swap keeps all of them honest, and browser history
 * stays clean (no POST-redirect).
 */
(function () {
  "use strict";

  function csrfToken() {
    const el = document.querySelector('input[name="csrf_token"]');
    return el ? el.value : "";
  }

  function flashError(msg) {
    let bar = document.getElementById("task-next-flash");
    if (!bar) {
      bar = document.createElement("div");
      bar.id = "task-next-flash";
      bar.className =
        "alert alert-danger position-fixed top-0 start-50 translate-middle-x mt-3 shadow";
      bar.style.zIndex = 2000;
      document.body.appendChild(bar);
    }
    bar.textContent = msg;
    setTimeout(() => { if (bar) bar.remove(); }, 3000);
  }

  function closeMenu(el) {
    const dd = el.closest(".task-next-group");
    const toggle = dd && dd.querySelector('[data-bs-toggle="dropdown"]');
    if (toggle && window.bootstrap) {
      window.bootstrap.Dropdown.getOrCreateInstance(toggle).hide();
    }
  }

  function refresh() {
    if (typeof window.bktodoRefreshMain === "function") {
      window.bktodoRefreshMain();
    } else {
      window.location.reload();
    }
  }

  /* POST the change. ``off`` takes the mark off, ``minutes`` sets the estimate
   * alongside marking it next, ``clearPlan`` also takes the task off the day
   * it was planned for. Returns a promise so the keyboard path can share it. */
  async function post(taskId, { off = false, minutes = null, clearPlan = false } = {}) {
    const fd = new FormData();
    fd.append("csrf_token", csrfToken());
    if (off) fd.append("off", "1");
    if (minutes) fd.append("minutes", String(minutes));
    if (clearPlan) fd.append("clear_plan", "1");
    try {
      const resp = await fetch(`/tasks/${taskId}/next`, {
        method: "POST",
        body: fd,
        credentials: "same-origin",
        headers: { "X-CSRFToken": csrfToken(), "X-Requested-With": "XMLHttpRequest" },
      });
      if (!resp.ok) {
        flashError(`Couldn't change the NEXT mark (${resp.status}).`);
        return false;
      }
      refresh();
      return true;
    } catch (err) {
      flashError("Couldn't change the NEXT mark — network error.");
      return false;
    }
  }

  // The arrow (and the "Remove NEXT" entry in the menu): flip the mark.
  document.addEventListener("click", (ev) => {
    const btn = ev.target.closest(".task-next-toggle");
    if (!btn) return;
    // The row is a link target and, in the kanban, a drag handle.
    ev.preventDefault();
    ev.stopPropagation();
    if (btn.dataset.busy === "1") return;
    btn.dataset.busy = "1";
    closeMenu(btn);
    post(btn.dataset.taskId, { off: btn.dataset.next === "1" });
  });

  // A duration in the menu: next + that estimate, one click.
  document.addEventListener("click", (ev) => {
    const btn = ev.target.closest(".task-next-dur");
    if (!btn) return;
    ev.preventDefault();
    ev.stopPropagation();
    if (btn.dataset.busy === "1") return;
    btn.dataset.busy = "1";
    closeMenu(btn);
    post(btn.dataset.taskId, { minutes: btn.dataset.minutes });
  });

  /* "Back to Next" on the Plan view: the day ran out on this task, so mark it
   * next AND take the stale plan date off it — one click, the same endpoint.
   *
   * CAPTURE phase on purpose. The button sits inside a card/pill that is itself
   * an inspector trigger (`[data-inspector-url]`), and inspector.js listens on
   * `document` too — `stopPropagation()` while bubbling does not stop another
   * listener on the SAME node, so the panel would fly open on every click.
   * Catching the click on the way down does stop it; it is the same trick the
   * note button beside it uses (event-notes.js). */
  document.addEventListener("click", (ev) => {
    const btn = ev.target.closest && ev.target.closest(".task-next-back");
    if (!btn) return;
    ev.preventDefault();
    ev.stopPropagation();
    if (btn.dataset.busy === "1") return;
    btn.dataset.busy = "1";
    closeMenu(btn);
    post(btn.dataset.taskId, { clearPlan: true });
  }, true);

  /* What the ``n`` key calls (shortcuts.js). Reads the row's own arrow so the
   * key and the click can never disagree about which way the toggle goes; a row
   * that has no arrow (a project, a closed task) is simply not toggled. */
  window.bktodoToggleNext = function (row) {
    if (!row) return false;
    const btn = row.querySelector(".task-next-toggle");
    const taskId = (btn && btn.dataset.taskId) || row.dataset.taskId;
    if (!taskId || !btn) return false;
    post(taskId, { off: btn.dataset.next === "1" });
    return true;
  };
})();
