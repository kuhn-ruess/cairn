/* Notes navigator: filter-by-typing + HTML5 drag to reparent notes/folders.
 * Scoped to #notes-nav; a no-op on pages without it. Drops POST to
 * /notes/<id>/move (parent = target folder, or top-level on the root row).
 */
(function () {
  function init() {
    const nav = document.getElementById("notes-nav");
    if (!nav || nav.dataset.wired === "1") return;
    nav.dataset.wired = "1";

    // --- filter ---
    const search = nav.querySelector(".nn-search");
    if (search) {
      const apply = () => {
        const q = search.value.trim().toLowerCase();
        const rows = Array.from(nav.querySelectorAll("[data-nn-name]"));
        // This box also drives the full-text search in the results column
        // (notes-list.js), so a word that only appears in a BODY matches
        // nothing here. Narrowing the tree to nothing in that case would take
        // the reader's navigation away just as the answer arrives beside it —
        // so when no name matches, the tree stays whole.
        const hit = (row) => !q || row.dataset.nnName.includes(q);
        const any = rows.some(hit);
        rows.forEach((row) => {
          const li = row.closest("li") || row;
          li.style.display = (!any || hit(row)) ? "" : "none";
        });
      };
      search.addEventListener("input", apply);
      search.addEventListener("keydown", (e) => {
        if (e.key === "Escape") { search.value = ""; apply(); }
      });
    }

    // --- drag to reparent ---
    let dragId = null;
    nav.querySelectorAll(".nn-li[data-note-id]").forEach((li) => { li.draggable = true; });

    nav.addEventListener("dragstart", (ev) => {
      const li = ev.target.closest && ev.target.closest(".nn-li[data-note-id]");
      if (!li) return;
      dragId = li.dataset.noteId;
      ev.dataTransfer.effectAllowed = "move";
      ev.dataTransfer.setData("text/plain", dragId);
      li.classList.add("nn-dragging");
    });
    nav.addEventListener("dragend", () => {
      nav.querySelectorAll(".nn-dragging, .nn-drop-hover")
        .forEach((el) => el.classList.remove("nn-dragging", "nn-drop-hover"));
      dragId = null;
    });
    nav.addEventListener("dragover", (ev) => {
      if (!dragId) return;
      const li = ev.target.closest && ev.target.closest(".nn-li[data-target-folder]");
      if (!li || li.dataset.noteId === dragId) return;
      ev.preventDefault();
      ev.dataTransfer.dropEffect = "move";
      nav.querySelectorAll(".nn-drop-hover").forEach((el) => el.classList.remove("nn-drop-hover"));
      li.classList.add("nn-drop-hover");
    });
    nav.addEventListener("drop", async (ev) => {
      if (!dragId) return;
      const li = ev.target.closest && ev.target.closest(".nn-li[data-target-folder]");
      if (!li || li.dataset.noteId === dragId) return;
      ev.preventDefault();
      const parent = li.dataset.targetFolder || "";  // "" = top level (root row)
      const fd = new FormData();
      const csrf = document.querySelector('input[name="csrf_token"]');
      if (csrf) fd.append("csrf_token", csrf.value);
      fd.append("parent_id", parent);
      try {
        const resp = await fetch(`/notes/${dragId}/move`, { method: "POST", body: fd });
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
