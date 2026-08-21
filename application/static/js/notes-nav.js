/* Notes navigator: the tree, and everything around it.
 *
 * Filter-by-typing narrows the tree to matching NAMES as you go; Enter takes
 * the same words to /notes/, which searches titles and bodies and shows the
 * hits as a page. So the box answers both "take me to the page called X" and
 * "where did I write about X", without a second list standing beside the tree
 * for the rest of the time.
 *
 * Also: HTML5 drag to reparent (POSTs /notes/<id>/move, parent = the target
 * folder or top-level on the root row), and the info-panel toggle.
 * Scoped to #notes-nav; a no-op on pages without it.
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
        // Enter takes the same words to the full-text search, so a word that
        // only appears in a BODY matches nothing here. Narrowing the tree to
        // nothing in that case would take the reader's navigation away right
        // when they are about to search properly — so when no name matches,
        // the tree stays whole.
        const hit = (row) => !q || row.dataset.nnName.includes(q);
        const any = rows.some(hit);
        rows.forEach((row) => {
          const li = row.closest("li") || row;
          li.style.display = (!any || hit(row)) ? "" : "none";
        });
      };
      search.addEventListener("input", apply);
      search.addEventListener("keydown", (e) => {
        if (e.key === "Escape") { search.value = ""; apply(); return; }
        if (e.key === "Enter") {
          e.preventDefault();
          const q = search.value.trim();
          window.location.href = q ? `/notes/?q=${encodeURIComponent(q)}` : "/notes/";
        }
      });
    }

    // --- tag filter: the notes carrying one tag, as a page ---
    const tag = nav.querySelector(".nn-tag");
    if (tag) {
      tag.addEventListener("change", () => {
        window.location.href = tag.value
          ? `/notes/?tag=${encodeURIComponent(tag.value)}` : "/notes/";
      });
    }

    // --- info panel: the one column that still folds away ---
    const INSPECTOR_KEY = "cairn.notes.inspectorOff";
    const wrap = document.querySelector(".notes-wrap");
    function applyInspector() {
      if (!wrap || !wrap.querySelector(".notes-inspector")) return;
      const stored = localStorage.getItem(INSPECTOR_KEY);
      // The note body needs ~600px to read comfortably, so a narrow window
      // folds the panel away until asked otherwise.
      const off = stored === null ? window.innerWidth < 1500 : stored === "1";
      wrap.classList.toggle("inspector-off", off);
      document.querySelectorAll('[data-toggle-column="inspector"]').forEach((b) => {
        b.classList.toggle("active", !off);
        b.setAttribute("aria-pressed", off ? "false" : "true");
      });
    }
    document.addEventListener("click", (e) => {
      if (!e.target.closest('[data-toggle-column="inspector"]') || !wrap) return;
      localStorage.setItem(INSPECTOR_KEY,
                           wrap.classList.contains("inspector-off") ? "0" : "1");
      applyInspector();
    });
    applyInspector();

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
