/* Project sidebar: filter input, drag-to-resize, collapse toggle.
 * Persistence is class-on-<html> + localStorage, applied synchronously
 * in base.html's head script. This file only wires the interactive
 * controls (toggle button, resize drag, filter input).
 */
(function () {
  const KEY_W = "cairn.projectSidebar.width";
  const KEY_COLLAPSED = "cairn.projectSidebar.collapsed";
  const HTML_CLASS = "cairn-ps-collapsed";

  function init() {
    const sidebar = document.getElementById("project-sidebar");
    if (!sidebar) return;
    if (sidebar.dataset.psWired === "1") return;
    sidebar.dataset.psWired = "1";

    const root = document.documentElement;

    // Filter rows by typing.
    const search = sidebar.querySelector(".ps-search");
    if (search) {
      const apply = () => {
        const q = search.value.trim().toLowerCase();
        sidebar.querySelectorAll("[data-project-name]").forEach((row) => {
          const li = row.closest("li") || row;
          const match = !q || row.dataset.projectName.includes(q);
          li.style.display = match ? "" : "none";
        });
      };
      search.addEventListener("input", apply);
      search.addEventListener("keydown", (e) => {
        if (e.key === "Escape") {
          search.value = "";
          apply();
        }
      });
    }

    // Collapse / expand toggle — flips a class on <html> and saves the
    // new state. The head script in base.html reads the same key on
    // every page load, so the state survives navigation.
    const toggle = sidebar.querySelector(".ps-toggle");
    if (toggle) {
      toggle.addEventListener("click", (e) => {
        e.preventDefault();
        const next = !root.classList.contains(HTML_CLASS);
        root.classList.toggle(HTML_CLASS, next);
        try { localStorage.setItem(KEY_COLLAPSED, next ? "1" : "0"); } catch (e) {}
      });
    }

    // Drag-to-resize via custom handle. CSS resize-handle was flaky
    // inside the flex parent — this gives us precise control.
    const handle = sidebar.querySelector(".ps-resizer");
    if (handle) {
      let startX = 0, startW = 0, dragging = false;
      const onMove = (e) => {
        if (!dragging) return;
        const x = (e.clientX || (e.touches && e.touches[0].clientX) || 0);
        const dx = x - startX;
        const w = Math.max(180, Math.min(600, startW + dx));
        root.style.setProperty("--project-sidebar-width", w + "px");
      };
      const onUp = () => {
        if (!dragging) return;
        dragging = false;
        document.body.classList.remove("ps-resizing");
        const w = parseInt(getComputedStyle(sidebar).width, 10);
        if (w) {
          try { localStorage.setItem(KEY_W, String(w)); } catch (e) {}
        }
        document.removeEventListener("mousemove", onMove);
        document.removeEventListener("mouseup", onUp);
        document.removeEventListener("touchmove", onMove);
        document.removeEventListener("touchend", onUp);
      };
      const onDown = (e) => {
        if (root.classList.contains(HTML_CLASS)) return;
        dragging = true;
        startX = e.clientX || (e.touches && e.touches[0].clientX) || 0;
        startW = parseInt(getComputedStyle(sidebar).width, 10) || 240;
        document.body.classList.add("ps-resizing");
        document.addEventListener("mousemove", onMove);
        document.addEventListener("mouseup", onUp);
        document.addEventListener("touchmove", onMove, { passive: false });
        document.addEventListener("touchend", onUp);
        e.preventDefault();
      };
      handle.addEventListener("mousedown", onDown);
      handle.addEventListener("touchstart", onDown, { passive: false });
    }

    // ---- HTML5 drag: project → project/folder = re-parent --------
    // Each ps-drop-target with a data-project-id can be dragged onto
    // another to set its parent. Drag onto the Inbox row to un-nest
    // (becomes top-level). The drop POSTs to /projects/<id>/move and
    // reloads on success.
    let dragPk = null;
    sidebar.querySelectorAll(".ps-drop-target[data-project-id]")
      .forEach((li) => { li.draggable = true; });

    sidebar.addEventListener("dragstart", (ev) => {
      const li = ev.target.closest && ev.target.closest(".ps-drop-target[data-project-id]");
      if (!li) return;
      dragPk = li.dataset.projectId;
      ev.dataTransfer.effectAllowed = "move";
      ev.dataTransfer.setData("text/plain", dragPk);
      li.classList.add("ps-row-dragging");
    });
    sidebar.addEventListener("dragend", (ev) => {
      const li = ev.target.closest && ev.target.closest(".ps-drop-target");
      if (li) li.classList.remove("ps-row-dragging");
      sidebar.querySelectorAll(".ps-drop-hover")
        .forEach((el) => el.classList.remove("ps-drop-hover"));
      dragPk = null;
    });
    sidebar.addEventListener("dragover", (ev) => {
      if (!dragPk) return;
      const li = ev.target.closest && ev.target.closest(".ps-drop-target");
      if (!li) return;
      if (li.dataset.projectId === dragPk) return;
      ev.preventDefault();
      ev.dataTransfer.dropEffect = "move";
      sidebar.querySelectorAll(".ps-drop-hover")
        .forEach((el) => el.classList.remove("ps-drop-hover"));
      li.classList.add("ps-drop-hover");
    });
    sidebar.addEventListener("drop", async (ev) => {
      if (!dragPk) return;
      const li = ev.target.closest && ev.target.closest(".ps-drop-target");
      if (!li) return;
      if (li.dataset.projectId === dragPk) return;
      ev.preventDefault();
      const newParent = li.dataset.projectId || "";  // empty = top-level (Inbox row)
      const fd = new FormData();
      const csrf = document.querySelector('input[name="csrf_token"]');
      if (csrf) fd.append("csrf_token", csrf.value);
      fd.append("parent_id", newParent);
      try {
        const resp = await fetch(`/projects/${dragPk}/move`,
                                 { method: "POST", body: fd });
        if (!resp.ok) {
          const j = await resp.json().catch(() => ({}));
          alert(j.error || `Move failed (${resp.status}).`);
          return;
        }
        window.location.reload();
      } catch (err) {
        alert("Move failed (network).");
      }
    });
  }

  if (document.readyState !== "loading") init();
  else document.addEventListener("DOMContentLoaded", init);
})();
