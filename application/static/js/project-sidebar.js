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
  }

  if (document.readyState !== "loading") init();
  else document.addEventListener("DOMContentLoaded", init);
})();
