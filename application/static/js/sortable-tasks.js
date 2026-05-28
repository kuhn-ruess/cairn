/* Drag-and-drop reordering / moving for any .task-list container.
 *
 * Each row carries data-task-id; each container may carry data-target-project
 * (the pk of the project the dropped task should belong to). If the container
 * has no data-target-project attribute, only sort_order changes on drop.
 *
 * Containers using the same group can accept items from each other — that
 * lets users drag tasks between sections inside the project-detail view.
 */
(function () {
  if (typeof Sortable === "undefined") return;

  function csrfToken() {
    const el = document.querySelector('input[name="csrf_token"]');
    return el ? el.value : "";
  }

  // Drag-merge state: when the cursor lingers over the centre of
  // another task row during a task drag for ``DWELL_MS``, that row
  // becomes a merge target. A short pass-through doesn't trigger merge
  // so reordering stays unaffected.
  const DWELL_MS = 450;
  let dragSource = null;
  let mergeTarget = null;
  let pendingMergeRow = null;
  let dwellTimer = null;
  function clearMergeTarget() {
    if (mergeTarget) mergeTarget.classList.remove("drop-merge-target");
    mergeTarget = null;
  }
  function cancelDwell() {
    if (dwellTimer) { clearTimeout(dwellTimer); dwellTimer = null; }
    if (pendingMergeRow) pendingMergeRow.classList.remove("drop-merge-pending");
    pendingMergeRow = null;
  }
  function setMergeTarget(row) {
    if (mergeTarget === row) return;
    clearMergeTarget();
    row.classList.add("drop-merge-target");
    mergeTarget = row;
  }
  function startDwell(row) {
    if (pendingMergeRow === row || mergeTarget === row) return;
    cancelDwell();
    pendingMergeRow = row;
    row.classList.add("drop-merge-pending");
    dwellTimer = setTimeout(() => {
      if (pendingMergeRow) pendingMergeRow.classList.remove("drop-merge-pending");
      if (pendingMergeRow) setMergeTarget(pendingMergeRow);
      pendingMergeRow = null;
      dwellTimer = null;
    }, DWELL_MS);
  }

  function init(el) {
    if (el.dataset.sortableInit) return;
    el.dataset.sortableInit = "1";
    const isKanban = !!el.closest(".kanban-board");
    const opts = {
      group: "bktodo-tasks",
      animation: 150,
      ghostClass: "drag-ghost",
      onStart: (ev) => {
        document.body.classList.add("is-dragging");
        dragSource = ev.item;
        clearMergeTarget();
        cancelDwell();
      },
      // onMove fires each time the dragged item would be inserted next
      // to ``ev.related``. We use it to start the dwell timer when the
      // cursor lingers over a real task row — that row becomes the
      // merge target if the user holds still long enough. Returning
      // ``true`` lets Sortable's normal reordering proceed; the merge
      // is resolved in onEnd.
      onMove: (ev) => {
        const related = ev.related;
        if (!related || !related.dataset || !related.dataset.taskId) {
          cancelDwell();
          return true;
        }
        if (dragSource && related === dragSource) {
          cancelDwell();
          return true;
        }
        startDwell(related);
        return true;
      },
      onEnd: (ev) => {
        document.body.classList.remove("is-dragging");
        const target = mergeTarget;
        const source = dragSource;
        clearMergeTarget();
        cancelDwell();
        dragSource = null;
        if (target && source && target !== source
            && target.dataset.taskId && source.dataset.taskId
            && target.dataset.taskId !== source.dataset.taskId) {
          // Drag-merge wins over the regular move: drop the source
          // under the target (which gets promoted to a project).
          mergeTasks(source, target);
          return;
        }
        onEnd(ev);
      },
    };
    if (isKanban) {
      // Kanban cards are draggable from anywhere on the card itself —
      // clicks on interactive children (the title link, buttons, etc.)
      // still work as clicks because preventOnFilter is false.
      opts.filter = "a, button, input, select, textarea, .form-check-input";
      opts.preventOnFilter = false;
    } else {
      opts.handle = ".task-handle";
    }
    new Sortable(el, opts);
  }

  // ---- Background refresh of <main> -------------------------------------
  // After any save or drag, we re-render just the main content area so
  // counters, sections and list contents stay in sync — the inspector and
  // bulk bar (outside <main>) keep their state.
  async function refreshMain() {
    try {
      const resp = await fetch(window.location.href, {
        headers: { "X-Requested-With": "background-refresh" },
        credentials: "same-origin",
      });
      if (!resp.ok) return;
      const html = await resp.text();
      const doc = new DOMParser().parseFromString(html, "text/html");
      const newMain = doc.querySelector("main");
      const main = document.querySelector("main");
      if (newMain && main) {
        main.innerHTML = newMain.innerHTML;
        main.querySelectorAll(".task-list, .tree-tasks").forEach(init);
      }
    } catch (err) {
      console.error("refresh failed", err);
    }
  }
  window.bktodoRefreshMain = refreshMain;
  document.addEventListener("bktodo:refresh-needed", refreshMain);

  async function mergeTasks(sourceRow, targetRow) {
    const fd = new FormData();
    fd.append("csrf_token", csrfToken());
    fd.append("source_id", sourceRow.dataset.taskId);
    fd.append("target_id", targetRow.dataset.taskId);
    try {
      const resp = await fetch("/tasks/merge", { method: "POST", body: fd });
      if (!resp.ok) {
        const detail = await resp.json().catch(() => ({ error: resp.status }));
        flashError(detail.error || "Merge failed");
        // Sortable already moved the source row; reload to fix the DOM.
        setTimeout(() => window.location.reload(), 800);
        return;
      }
      const payload = await resp.json();
      const wrap = document.createElement("div");
      wrap.innerHTML = payload.html.trim();
      const newBlock = wrap.firstElementChild;
      // Drop the two original rows and put the merged block where the
      // target sat. Initialise SortableJS on its inner .task-list so
      // more tasks can be dragged in immediately.
      const slot = targetRow.parentNode;
      slot.insertBefore(newBlock, targetRow);
      sourceRow.remove();
      targetRow.remove();
      newBlock.querySelectorAll(".task-list, .tree-tasks").forEach(init);
      const nameInput = newBlock.querySelector(".merged-name-input");
      if (nameInput) {
        nameInput.focus();
        nameInput.select();
      }
    } catch (err) {
      flashError("Merge failed (network).");
      setTimeout(() => window.location.reload(), 800);
    }
  }

  // Inline rename on the merged-block name input. Persists on blur or
  // Enter; Escape reverts to the last saved value.
  document.addEventListener("change", (ev) => {
    const input = ev.target.closest(".merged-name-input");
    if (!input) return;
    saveMergedName(input);
  });
  document.addEventListener("keydown", (ev) => {
    const input = ev.target.closest(".merged-name-input");
    if (!input) return;
    if (ev.key === "Enter") { ev.preventDefault(); input.blur(); }
    if (ev.key === "Escape") { ev.preventDefault(); input.value = input.dataset.lastSaved || ""; input.blur(); }
  });
  // Collapse / expand sub-tasks under a parent. Persists via the
  // backend so the state survives reload + comes back on other devices.
  document.addEventListener("click", (ev) => {
    const btn = ev.target.closest(".task-collapse-toggle");
    if (!btn) return;
    ev.preventDefault();
    ev.stopPropagation();
    const id = btn.dataset.itemId;
    const fd = new FormData();
    fd.append("csrf_token", csrfToken());
    fetch(`/tasks/${id}/toggle-collapse`, { method: "POST", body: fd })
      .then((r) => r.ok ? r.json() : null)
      .then((json) => {
        if (!json) return;
        // Server-driven refresh of just <main> keeps scroll + inspector
        // state intact while picking up the new visibility.
        if (typeof window.bktodoRefreshMain === "function") {
          window.bktodoRefreshMain();
        } else {
          window.location.reload();
        }
      })
      .catch(() => flashError("Collapse toggle failed"));
  });

  // Task-complete toggle without page reload — POST the existing form,
  // ignore the redirect, patch the DOM in place.
  document.addEventListener("submit", (ev) => {
    const form = ev.target.closest("form[data-task-complete-form]");
    if (!form) return;
    ev.preventDefault();
    const row = form.closest(".task-row");
    const wasDone = row && row.classList.contains("done");
    const fd = new FormData(form);
    fetch(form.action, {
      method: "POST",
      body: fd,
      credentials: "same-origin",
      redirect: "manual",
    }).then((resp) => {
      // 'manual' returns an opaqueredirect on 3xx but the request was
      // actually executed — patch the DOM regardless.
      if (!resp || (resp.type !== "opaqueredirect" && !resp.ok)) {
        flashError("Toggle failed");
        return;
      }
      if (!row) return;
      // Fade the row out and drop it from the list — no perspective in
      // the app shows both done and open together, so the row's new
      // status no longer matches the current view either way. The
      // page reload on next navigation gets fresh data.
      row.classList.add("task-row-leaving");
      setTimeout(() => { row.remove(); }, 220);
    }).catch(() => flashError("Toggle failed (network)"));
  });

  async function saveMergedName(input) {
    const name = (input.value || "").trim();
    if (!name || name === input.dataset.lastSaved) return;
    const fd = new FormData();
    fd.append("csrf_token", csrfToken());
    fd.append("name", name);
    try {
      const resp = await fetch(`/projects/${input.dataset.projectId}/rename`,
                               { method: "POST", body: fd });
      if (resp.ok) input.dataset.lastSaved = name;
    } catch (err) {
      flashError("Rename failed");
    }
  }

  async function onEnd(ev) {
    const row = ev.item;
    // Sub-project rows take a separate API path — re-parent the project
    // instead of moving a task.
    if (row.dataset.spId) {
      const fd = new FormData();
      fd.append("csrf_token", csrfToken());
      const parentId = ev.to.dataset.targetProject || "";
      fd.append("parent_id", parentId);
      // Renumber sort_order across every sub-project currently in the
      // destination container, in the new visual order.
      Array.from(ev.to.querySelectorAll("[data-sp-id]"))
        .map((el) => el.dataset.spId)
        .filter(Boolean)
        .forEach((id) => fd.append("sp_siblings[]", id));
      try {
        const resp = await fetch(`/projects/${row.dataset.spId}/move`, {
          method: "POST", body: fd,
        });
        if (!resp.ok) {
          flashError(`Move failed (${resp.status}). Reloading…`);
          setTimeout(() => window.location.reload(), 1000);
        }
      } catch (err) {
        flashError("Move failed. Reloading…");
        setTimeout(() => window.location.reload(), 1000);
      }
      return;
    }
    const taskId = row.dataset.taskId;
    if (!taskId) return;

    // Collect the new order of every sibling in the destination container
    // so the server can renumber sort_order across the board. Without this
    // we'd only update the moved task and the order wouldn't survive a
    // reload (existing siblings keep their old, possibly-conflicting
    // sort_orders).
    const siblingIds = Array.from(ev.to.querySelectorAll("[data-task-id]"))
      .map((el) => el.dataset.taskId)
      .filter(Boolean);

    const fd = new FormData();
    fd.append("csrf_token", csrfToken());
    siblingIds.forEach((id) => fd.append("siblings[]", id));
    if (ev.to.dataset.targetProject !== undefined) {
      fd.append("project_id", ev.to.dataset.targetProject);
    }
    if (ev.to.dataset.targetStatus !== undefined) {
      fd.append("status", ev.to.dataset.targetStatus);
    }

    try {
      const resp = await fetch(`/tasks/${taskId}/move`, {
        method: "POST",
        body: fd,
        headers: { "X-Inspector-Request": "1" },
      });
      if (!resp.ok) {
        const detail = await resp.text();
        console.error("move failed", resp.status, detail.slice(0, 200));
        flashError(`Move failed (${resp.status}). Reloading…`);
        setTimeout(() => window.location.reload(), 1200);
        return;
      }
      // Refresh main so counters + cross-section moves reflect immediately.
      refreshMain();
    } catch (err) {
      console.error(err);
      flashError("Move failed — network error.");
      setTimeout(() => window.location.reload(), 1200);
    }
  }

  function flashError(msg) {
    let bar = document.getElementById("move-error-flash");
    if (!bar) {
      bar = document.createElement("div");
      bar.id = "move-error-flash";
      bar.className =
        "alert alert-danger position-fixed top-0 start-50 translate-middle-x mt-3 shadow";
      bar.style.zIndex = 2000;
      document.body.appendChild(bar);
    }
    bar.textContent = msg;
  }

  document.querySelectorAll(".task-list, .tree-tasks").forEach(init);
})();
