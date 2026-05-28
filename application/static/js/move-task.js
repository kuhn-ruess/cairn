/* "Move task to project" modal — opens via the ``m`` shortcut while a
 * task row is hovered. Seeds with same-level projects from
 * /tasks/<id>/move-options; typing live-searches all projects via
 * /tasks/quickjump (kind=project|folder). Enter or click moves the
 * task and removes its row from the current view.
 */
(function () {
  const modalEl = document.getElementById("moveTaskModal");
  if (!modalEl) return;
  const input = document.getElementById("moveTaskSearch");
  const results = document.getElementById("moveTaskResults");
  const titleEl = document.getElementById("moveTaskTitle");
  const csrfEl = document.getElementById("moveTaskCsrf");

  let bs = null;
  let currentTaskId = null;
  let currentTaskRow = null;
  let activeIndex = 0;
  let items = [];   // [{id, name, kind, parent}]
  let suggestions = [];  // seeded same-level
  let searchSeq = 0;
  let searchTimer = null;

  function csrf() {
    return csrfEl ? csrfEl.value :
      (document.querySelector('input[name="csrf_token"]')?.value || "");
  }

  function renderItems(list) {
    results.innerHTML = "";
    items = list || [];
    activeIndex = 0;
    if (!items.length) {
      results.innerHTML = '<li class="text-muted small px-2 py-1">No matching projects.</li>';
      return;
    }
    items.forEach((it, i) => {
      const li = document.createElement("li");
      li.className = "quickjump-item move-task-result";
      li.dataset.index = i;
      const kindIcon = it.kind === "folder" ? "bi-folder2-open text-warning"
                     : it.kind === "section" ? "bi-bookmark text-info"
                     : "bi-folder text-muted";
      // Compact one-liner: icon + name + ghosted full path, all on
      // the same row so dense lists stay scannable.
      const path = it.path || it.parent || "";
      li.innerHTML =
        `<i class="bi ${kindIcon}"></i>` +
        `<span class="quickjump-title">${escapeHtml(it.name)}</span>` +
        (path ? `<span class="quickjump-sub">${escapeHtml(path)}</span>` : "");
      li.addEventListener("click", () => commit(i));
      results.appendChild(li);
    });
    updateActive();
  }

  function escapeHtml(s) {
    return (s || "").replace(/[&<>"']/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;",
      '"': "&quot;", "'": "&#39;",
    })[c]);
  }

  function updateActive() {
    results.querySelectorAll(".quickjump-item").forEach((li, i) => {
      li.classList.toggle("active", i === activeIndex);
    });
    const active = results.children[activeIndex];
    if (active && active.scrollIntoView) {
      active.scrollIntoView({ block: "nearest" });
    }
  }

  async function fetchSuggestions(taskId) {
    try {
      const resp = await fetch(`/tasks/${taskId}/move-options`,
                               { credentials: "same-origin" });
      if (!resp.ok) return { suggestions: [] };
      return await resp.json();
    } catch (err) {
      return { suggestions: [] };
    }
  }

  async function liveSearch(q) {
    const seq = ++searchSeq;
    if (!q || q.length < 2) {
      renderItems(suggestions);
      return;
    }
    if (!currentTaskId) return;
    try {
      const url = `/tasks/${currentTaskId}/move-options?q=${encodeURIComponent(q)}`;
      const resp = await fetch(url, { credentials: "same-origin" });
      if (!resp.ok) return;
      const data = await resp.json();
      if (seq !== searchSeq) return;  // stale response
      renderItems(data.suggestions || []);
    } catch (err) { /* swallow */ }
  }

  async function commit(index) {
    const target = items[index];
    if (!target || !currentTaskId) return;
    const fd = new FormData();
    fd.append("csrf_token", csrf());
    fd.append("project_id", target.id);
    try {
      const resp = await fetch(`/tasks/${currentTaskId}/move`, {
        method: "POST",
        body: fd,
        headers: { "X-Inspector-Request": "1" },
      });
      if (!resp.ok) {
        alert(`Move failed (${resp.status}).`);
        return;
      }
      // Remove the task row from the current view — it now lives
      // somewhere else, so showing it here would be stale.
      if (currentTaskRow && currentTaskRow.isConnected) {
        currentTaskRow.classList.add("task-row-leaving");
        setTimeout(() => currentTaskRow.remove(), 200);
      }
      bs.hide();
    } catch (err) {
      alert("Move failed (network).");
    }
  }

  async function open(taskId, taskRow) {
    if (!taskId) return;
    currentTaskId = taskId;
    currentTaskRow = taskRow;
    activeIndex = 0;
    items = [];
    suggestions = [];
    input.value = "";
    results.innerHTML = '<li class="text-muted small px-2 py-1">Loading…</li>';
    const title = (
      taskRow?.querySelector(".task-title")?.textContent ||
      taskRow?.querySelector("a")?.textContent || ""
    ).trim();
    titleEl.textContent = title;
    bs = bootstrap.Modal.getOrCreateInstance(modalEl);
    bs.show();
    const data = await fetchSuggestions(taskId);
    suggestions = data.suggestions || [];
    renderItems(suggestions);
    setTimeout(() => input.focus(), 50);
  }
  window.bktodoOpenMoveTaskModal = open;

  input.addEventListener("input", () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => liveSearch(input.value.trim()), 120);
  });
  input.addEventListener("keydown", (e) => {
    if (e.key === "ArrowDown") {
      e.preventDefault();
      activeIndex = Math.min(items.length - 1, activeIndex + 1);
      updateActive();
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      activeIndex = Math.max(0, activeIndex - 1);
      updateActive();
    } else if (e.key === "Enter") {
      e.preventDefault();
      commit(activeIndex);
    }
  });
})();
