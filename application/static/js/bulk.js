/* Bulk selection + action bar.
 *
 * Each selectable row exposes a checkbox with .bulk-check and a value=<pk>.
 * The bar (#bulk-bar) shows when at least one box is checked and submits the
 * collected IDs as repeated `task_ids[]` (or `project_ids[]`) fields to the
 * form's action.
 */
(function () {
  const bar = document.getElementById("bulk-bar");
  if (!bar) return;
  const countEl = bar.querySelector(".bulk-count");
  const idsContainer = bar.querySelector(".bulk-id-container");
  const actionInput = bar.querySelector(".bulk-action-input");
  const form = bar.querySelector("form");

  // Detect which kind of list we're on: tasks or projects.
  function detectScope() {
    const checked = document.querySelector(".bulk-check:checked");
    if (!checked) return null;
    return checked.dataset.bulkScope || "tasks";
  }

  function refreshBar() {
    const checked = document.querySelectorAll(".bulk-check:checked");
    if (!checked.length) {
      bar.classList.add("d-none");
      return;
    }
    bar.classList.remove("d-none");
    countEl.textContent = checked.length;

    const scope = detectScope();
    bar.dataset.bulkScope = scope;
    const idsField = scope === "projects" ? "project_ids[]" : "task_ids[]";
    // NB: ``form.action`` resolves to the <input name="action"> child
    // because of HTML's name-collision rule. Use getAttribute/setAttribute
    // to read and write the actual form action URL.
    const targetAction =
      scope === "projects" ? bar.dataset.projectsAction : bar.dataset.tasksAction;
    if (targetAction) form.setAttribute("action", targetAction);

    idsContainer.innerHTML = "";
    checked.forEach((c) => {
      const input = document.createElement("input");
      input.type = "hidden";
      input.name = idsField;
      input.value = c.value;
      idsContainer.appendChild(input);
    });

    // Show buttons whose data-bulk-show includes the current scope.
    bar.querySelectorAll("[data-bulk-show]").forEach((el) => {
      const allowed = (el.dataset.bulkShow || "").split(",").map((s) => s.trim());
      el.classList.toggle("d-none", !allowed.includes(scope));
    });
  }

  document.addEventListener("change", (ev) => {
    if (ev.target.classList.contains("bulk-check")) {
      syncRowClass(ev.target);
      refreshBar();
    }
  });

  // ---- Invisible selection: Cmd/Ctrl-click toggles, Shift-click range ----
  let lastIndex = null;

  function rowOf(cb) {
    return cb.closest(".task-row, tr");
  }
  function syncRowClass(cb) {
    const row = rowOf(cb);
    if (row) row.classList.toggle("selected", cb.checked);
  }
  function checkboxesInScope(scope) {
    return Array.from(
      document.querySelectorAll(`.bulk-check[data-bulk-scope="${scope}"]`)
    );
  }
  function findCheckbox(ev) {
    const row = ev.target.closest(".task-row, tr");
    if (!row) return null;
    return row.querySelector(".bulk-check");
  }

  // Capture phase so we beat link defaults (Cmd-click would otherwise
   // open a new tab) and other bubble-phase handlers.
  document.addEventListener("click", (ev) => {
    if (!(ev.metaKey || ev.ctrlKey || ev.shiftKey)) return;
    if (ev.target.closest("button, input, textarea, select, .bulk-bar, .offcanvas")) return;
    const cb = findCheckbox(ev);
    if (!cb) return;
    ev.preventDefault();
    ev.stopPropagation();

    const scope = cb.dataset.bulkScope || "tasks";
    const all = checkboxesInScope(scope);
    const targetIndex = all.indexOf(cb);

    if (ev.shiftKey && lastIndex != null && lastIndex !== targetIndex) {
      const [a, b] = [lastIndex, targetIndex].sort((x, y) => x - y);
      for (let i = a; i <= b; i++) {
        all[i].checked = true;
        syncRowClass(all[i]);
      }
    } else {
      cb.checked = !cb.checked;
      syncRowClass(cb);
      lastIndex = targetIndex;
    }
    refreshBar();
  }, true);  // capture phase

  // ---- Keyboard ----
  function selectedTaskRows() {
    return Array.from(document.querySelectorAll(".bulk-check:checked"))
      .map((c) => c.closest(".task-row, tr"))
      .filter(Boolean);
  }

  function submitBulkStatus(status, taskIds = null) {
    submitBulk("set_status", { status }, taskIds);
  }
  function submitBulkFlag(taskIds = null) {
    submitBulk("toggle_flag", {}, taskIds);
  }
  function submitBulkDelete(taskIds = null) {
    submitBulk("delete", {}, taskIds);
  }

  // ---- Hover-target tracking so shortcuts work without first selecting ----
  let hoveredRow = null;
  document.addEventListener("mouseover", (ev) => {
    const row = ev.target.closest(".task-row[data-task-id]");
    if (row) hoveredRow = row;
  });
  document.addEventListener("mouseout", (ev) => {
    const row = ev.target.closest(".task-row[data-task-id]");
    if (row && hoveredRow === row && ev.relatedTarget &&
        !ev.relatedTarget.closest(".task-row[data-task-id]")) {
      hoveredRow = null;
    }
  });

  function targetRows() {
    const sel = selectedTaskRows().filter((r) => r.dataset.taskStatus !== undefined);
    if (sel.length) return sel;
    return hoveredRow ? [hoveredRow] : [];
  }

  document.addEventListener("keydown", (ev) => {
    if (ev.target.matches("input, textarea, select, [contenteditable]")) return;

    if (ev.key === "Escape") {
      const checked = document.querySelectorAll(".bulk-check:checked");
      if (!checked.length) return;
      checked.forEach((c) => {
        c.checked = false;
        syncRowClass(c);
      });
      lastIndex = null;
      refreshBar();
      return;
    }

    if ((ev.metaKey || ev.ctrlKey) && ev.key.toLowerCase() === "a") {
      const visibleScope = (document.querySelector(".bulk-check") || {}).dataset
        ?.bulkScope;
      if (!visibleScope) return;
      ev.preventDefault();
      checkboxesInScope(visibleScope).forEach((c) => {
        c.checked = true;
        syncRowClass(c);
      });
      refreshBar();
      return;
    }

    if (ev.key === "?") {
      ev.preventDefault();
      window.location.href = "/help";
      return;
    }

    // Single-letter shortcuts act on the current selection, or — when
    // nothing is selected — the row currently under the cursor. So just
    // hovering a task and pressing `n` toggles it as Next.
    if (ev.metaKey || ev.ctrlKey || ev.altKey || ev.shiftKey) return;
    const rows = targetRows();
    if (!rows.length) return;
    const ids = rows.map((r) => r.dataset.taskId).filter(Boolean);
    if (!ids.length) return;
    const k = ev.key.toLowerCase();

    if (k === "n") {
      ev.preventDefault();
      submitBulkStatus("next", ids);
    } else if (k === "w") {
      ev.preventDefault();
      submitBulkStatus("waiting", ids);
    } else if (k === "s") {
      ev.preventDefault();
      submitBulkStatus("someday", ids);
    } else if (k === "f") {
      ev.preventDefault();
      submitBulkFlag(ids);
    } else if (k === "delete" || ev.key === "Backspace") {
      ev.preventDefault();
      if (confirm(`Delete ${ids.length} task(s)?`)) submitBulkDelete(ids);
    }
  });

  // Cache initial action URLs so we can flip between scopes. Read via
  // getAttribute because ``form.action`` is shadowed by the inner
  // <input name="action">.
  bar.dataset.tasksAction = form.getAttribute("action") || "";
  const projectsFormEl = document.getElementById("bulk-projects-bar-form");
  if (projectsFormEl) {
    bar.dataset.projectsAction = projectsFormEl.getAttribute("action") || "";
  }

  function submitReview(days) {
    submitBulk("set_review", { review_interval_days: String(days) });
  }

  // -------- Universal bulk submit via fetch -------------------------
  // We build FormData from the live selection so there's no chance for
  // hidden inputs in the form to go stale or get lost between renders.
  async function submitBulk(action, extras = {}, overrideTaskIds = null) {
    const scope = detectScope() || bar.dataset.bulkScope || "tasks";
    const url = scope === "projects" ? bar.dataset.projectsAction : bar.dataset.tasksAction;
    if (!url) return;
    const idsField = scope === "projects" ? "project_ids[]" : "task_ids[]";
    const ids = overrideTaskIds && overrideTaskIds.length
      ? overrideTaskIds
      : Array.from(document.querySelectorAll(`.bulk-check[data-bulk-scope="${scope}"]:checked`))
          .map((c) => c.value);
    if (!ids.length) return;

    const fd = new FormData();
    const csrfEl = document.querySelector('input[name="csrf_token"]');
    if (csrfEl) fd.append("csrf_token", csrfEl.value);
    fd.append("action", action);
    for (const [k, v] of Object.entries(extras)) fd.append(k, v);
    ids.forEach((id) => fd.append(idsField, id));

    const banner = showBanner(`Sending ${action} for ${ids.length} item(s)…`, "info");
    try {
      const resp = await fetch(url, {
        method: "POST",
        body: fd,
        redirect: "follow",
        credentials: "same-origin",
      });
      if (!resp.ok && !resp.redirected) {
        const detail = await resp.text();
        banner.remove();
        showBanner(
          `Bulk ${action} failed (${resp.status}). ${detail.slice(0, 200)}`,
          "danger",
          6000
        );
        return;
      }
      window.location.href = resp.url || window.location.href;
    } catch (err) {
      banner.remove();
      showBanner(`Network error: ${err.message}`, "danger", 6000);
    }
  }

  function showBanner(msg, kind = "info", autoDismissMs = 0) {
    const el = document.createElement("div");
    el.className =
      `alert alert-${kind} position-fixed top-0 start-50 translate-middle-x mt-3 shadow`;
    el.style.zIndex = 2100;
    el.textContent = msg;
    document.body.appendChild(el);
    if (autoDismissMs) setTimeout(() => el.remove(), autoDismissMs);
    return el;
  }

  bar.addEventListener("click", (ev) => {
    const btn = ev.target.closest("[data-bulk-action]");
    if (btn) {
      const action = btn.dataset.bulkAction;
      const confirmMsg = btn.dataset.bulkConfirm;
      if (confirmMsg && !confirm(confirmMsg)) return;
      submitBulk(action);
      return;
    }

    const statusItem = ev.target.closest(".bulk-status-item");
    if (statusItem) {
      ev.preventDefault();
      submitBulk("set_status", { status: statusItem.dataset.status });
      return;
    }

    const priorityItem = ev.target.closest(".bulk-priority-item");
    if (priorityItem) {
      ev.preventDefault();
      submitBulk("set_priority", { priority: priorityItem.dataset.priority });
      return;
    }

    const reviewItem = ev.target.closest(".bulk-review-item");
    if (reviewItem) {
      ev.preventDefault();
      submitReview(reviewItem.dataset.reviewDays);
      return;
    }

    const reviewCustom = ev.target.closest(".bulk-review-custom");
    if (reviewCustom) {
      ev.preventDefault();
      const raw = prompt(
        "Review every how often?\n" +
        "Enter a number followed by d, w, m or y (e.g. 3w, 2m, 14d):",
        "2w"
      );
      if (!raw) return;
      const m = raw.trim().toLowerCase().match(/^(\d+)\s*([dwmy]?)$/);
      if (!m) {
        alert("Couldn't parse '" + raw + "'. Try '7d', '2w', '3m', '1y'.");
        return;
      }
      const n = parseInt(m[1], 10);
      const unit = m[2] || "d";
      const days = n * ({ d: 1, w: 7, m: 30, y: 365 }[unit] || 1);
      submitReview(days);
      return;
    }

    const moveItem = ev.target.closest(".bulk-move-item");
    if (moveItem) {
      ev.preventDefault();
      submitBulk("set_project", { project_id: moveItem.dataset.projectId || "" });
      return;
    }

    if (ev.target.closest("[data-bulk-clear]")) {
      ev.preventDefault();
      document.querySelectorAll(".bulk-check:checked").forEach((c) => {
        c.checked = false;
        syncRowClass(c);
      });
      lastIndex = null;
      refreshBar();
    }
  });
})();
