/* Tree-page behaviours: live filter, quick-add (project/folder),
 * one-click convert-to-task on empty projects, and project drag-drop
 * (reorder + reparent via the existing /projects/<id>/move endpoint).
 *
 * Task drag-drop is wired up by sortable-tasks.js — this file only
 * adds the project sortable layer and the tree-specific actions.
 *
 * Note on load order: this script is included from inside <main>, but
 * SortableJS, sortable-tasks.js and the inspector live at the bottom
 * of <body>. We defer setup to DOMContentLoaded so those are in place
 * when we initialise — otherwise the filter / quick-add / convert
 * bindings get silently skipped.
 */
(function () {
  function csrfToken() {
    const el = document.querySelector('input[name="csrf_token"]');
    return el ? el.value : "";
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
    setTimeout(() => { if (bar) bar.remove(); }, 3000);
  }

  // ---- Project drag-drop (separate Sortable group) ---------------------
  function initProjectSortables(root) {
    (root || document).querySelectorAll(".tree-projects").forEach((el) => {
      if (el.dataset.projectSortableInit) return;
      el.dataset.projectSortableInit = "1";
      new Sortable(el, {
        group: "bktodo-tree-projects",
        animation: 150,
        handle: ".project-handle",
        draggable: ".tree-project",
        ghostClass: "drag-ghost",
        // Without this, dropping a project inside another project's
        // nested ``.tree-projects`` would let the user re-nest into a
        // descendant — sortable handles the visual; we still validate
        // server-side for cycles.
        onStart: () => document.body.classList.add("is-dragging"),
        onEnd: async (ev) => {
          document.body.classList.remove("is-dragging");
          const row = ev.item;
          const id = row.dataset.spId;
          if (!id) return;
          const parentId = ev.to.dataset.targetProject || "";
          const fd = new FormData();
          fd.append("csrf_token", csrfToken());
          fd.append("parent_id", parentId);
          Array.from(ev.to.querySelectorAll(":scope > .tree-project"))
            .map((n) => n.dataset.spId)
            .filter(Boolean)
            .forEach((sid) => fd.append("sp_siblings[]", sid));
          try {
            const resp = await fetch(`/projects/${id}/move`, {
              method: "POST", body: fd,
            });
            if (!resp.ok) {
              const detail = await resp.json().catch(() => ({}));
              flashError(detail.error || `Move failed (${resp.status}).`);
              if (typeof window.bktodoRefreshMain === "function") {
                window.bktodoRefreshMain();
              }
              return;
            }
            // Repaint so empty-project state and the "convert to task"
            // button stay accurate after a move.
            if (typeof window.bktodoRefreshMain === "function") {
              window.bktodoRefreshMain();
            }
          } catch (err) {
            flashError("Move failed (network).");
          }
        },
      });
    });
  }

  // ---- Live filter -----------------------------------------------------
  // Hides tasks and projects whose text doesn't match the query, but
  // keeps ancestors of matches visible so the user can see WHERE the
  // matching items live and still drag them around.
  function applyFilter(query) {
    const q = (query || "").trim().toLowerCase();
    const root = document.querySelector(".tree-root");
    if (!root) return;

    // Reset visibility classes.
    root.querySelectorAll(".tree-filter-hidden").forEach((el) =>
      el.classList.remove("tree-filter-hidden"));
    root.querySelectorAll(".tree-filter-match").forEach((el) =>
      el.classList.remove("tree-filter-match"));

    if (!q) return;  // empty filter shows everything

    // First pass: mark every item that matches the query.
    const matchSelector = "[data-filter-text]";
    root.querySelectorAll(matchSelector).forEach((el) => {
      const text = el.dataset.filterText || "";
      if (text.includes(q)) el.classList.add("tree-filter-match");
    });

    // Promote ancestors: any element containing a match stays visible.
    root.querySelectorAll(".tree-filter-match").forEach((el) => {
      let cur = el.parentElement;
      while (cur && cur !== root) {
        if (cur.matches && cur.matches(".tree-project, .tree-task")) {
          cur.classList.add("tree-filter-match");
        }
        cur = cur.parentElement;
      }
    });

    // Hide anything that didn't match and isn't an ancestor of a match.
    root.querySelectorAll(".tree-project, .tree-task").forEach((el) => {
      if (!el.classList.contains("tree-filter-match")) {
        el.classList.add("tree-filter-hidden");
      }
    });
  }

  function bindFilter() {
    const input = document.getElementById("tree-filter");
    const clearBtn = document.getElementById("tree-filter-clear");
    if (!input) return;
    input.addEventListener("input", () => applyFilter(input.value));
    if (clearBtn) {
      clearBtn.addEventListener("click", () => {
        input.value = "";
        applyFilter("");
        input.focus();
      });
    }
  }

  // ---- Quick-add (toolbar form) ----------------------------------------
  async function quickAdd({ name, kind, parentId }) {
    const fd = new FormData();
    fd.append("csrf_token", csrfToken());
    fd.append("name", name);
    fd.append("kind", kind);
    fd.append("parent_id", parentId || "");
    try {
      const resp = await fetch("/tree/api/quick-add", { method: "POST", body: fd });
      const data = await resp.json().catch(() => ({}));
      if (!resp.ok) {
        flashError(data.error || `Create failed (${resp.status}).`);
        return false;
      }
      if (typeof window.bktodoRefreshMain === "function") {
        window.bktodoRefreshMain();
      }
      return true;
    } catch (err) {
      flashError("Create failed (network).");
      return false;
    }
  }

  function bindQuickAdd() {
    const form = document.getElementById("tree-quickadd-form");
    if (!form) return;
    // Track which submit button was clicked so we know whether the user
    // wants a project or a folder.
    form.querySelectorAll('button[type="submit"]').forEach((btn) => {
      btn.addEventListener("click", () => {
        form.querySelector('input[name="kind"]').value = btn.dataset.kind || "project";
      });
    });
    form.addEventListener("submit", async (ev) => {
      ev.preventDefault();
      const nameInput = form.querySelector('input[name="name"]');
      const name = (nameInput.value || "").trim();
      if (!name) return;
      const kind = form.querySelector('input[name="kind"]').value || "project";
      const ok = await quickAdd({ name, kind, parentId: "" });
      if (ok) { nameInput.value = ""; nameInput.focus(); }
    });
  }

  // ---- "Add inside folder" dropdown items ------------------------------
  function bindFolderQuickAdd() {
    document.addEventListener("click", async (ev) => {
      const link = ev.target.closest(".tree-quickadd-here");
      if (!link) return;
      ev.preventDefault();
      const parentId = link.dataset.parentId || "";
      const kind = link.dataset.kind || "project";
      const name = window.prompt(`New ${kind} name:`);
      if (!name || !name.trim()) return;
      await quickAdd({ name: name.trim(), kind, parentId });
    });
  }

  // ---- One-click convert empty project → task --------------------------
  function bindConvertToTask() {
    document.addEventListener("click", async (ev) => {
      const btn = ev.target.closest(".tree-convert-to-task-btn");
      if (!btn) return;
      ev.preventDefault();
      ev.stopPropagation();
      const id = btn.dataset.projectId;
      if (!id) return;
      const fd = new FormData();
      fd.append("csrf_token", csrfToken());
      try {
        const resp = await fetch(`/projects/${id}/convert-to-task`, {
          method: "POST",
          body: fd,
          headers: { "X-Requested-With": "XMLHttpRequest" },
        });
        if (!resp.ok) {
          flashError(`Convert failed (${resp.status}).`);
          return;
        }
        if (typeof window.bktodoRefreshMain === "function") {
          window.bktodoRefreshMain();
        }
      } catch (err) {
        flashError("Convert failed (network).");
      }
    });
  }

  function init() {
    // Re-init project sortables after every <main> refresh (the task code
    // already re-initialises .tree-tasks for us).
    document.addEventListener("bktodo:refresh-needed", () => {
      setTimeout(() => initProjectSortables(document), 0);
    });
    // Wrap bktodoRefreshMain so we also re-init project sortables once
    // the new DOM has been swapped in.
    const origRefresh = window.bktodoRefreshMain;
    if (typeof origRefresh === "function") {
      window.bktodoRefreshMain = async function () {
        await origRefresh();
        initProjectSortables(document);
        // Re-apply the active filter to the new DOM.
        const input = document.getElementById("tree-filter");
        if (input && input.value) applyFilter(input.value);
      };
    }
    initProjectSortables(document);
    bindFilter();
    bindQuickAdd();
    bindFolderQuickAdd();
    bindConvertToTask();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
