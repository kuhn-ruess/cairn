/* Notes navigator: the tree, and everything around it.
 *
 * The search box searches — titles AND bodies, on the server — and shows the
 * hits underneath itself, each with the snippet that says where it matched.
 * It does not touch the tree. Making one box do both was how "nothing matched
 * any name" ended up looking like "everything is a match": with nothing left
 * to show, the tree came back whole.
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

    // --- search ---
    const search = nav.querySelector(".nn-search");
    const results = nav.querySelector(".nn-results");
    if (search && results) {
      let hits = [], total = 0, active = -1, timer = null, seq = 0;

      const esc = (t) => (t || "").replace(/[&<>"']/g, (c) =>
        ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

      function hide() { results.style.display = "none"; active = -1; }

      function render() {
        if (!hits.length) {
          results.innerHTML =
            '<div class="nn-result text-muted small">Nothing found.</div>';
        } else {
          results.innerHTML = hits.map((h, i) => (
            `<a class="nn-result list-group-item list-group-item-action ${i === active ? "active" : ""}"
                href="/notes/${encodeURIComponent(h.id)}" data-i="${i}">
               <div class="nn-result-title">${esc(h.title)}</div>
               ${h.folder ? `<div class="nn-result-where">${esc(h.folder)}</div>` : ""}
               <div class="nn-result-snippet">${h.snippet}</div>
             </a>`)).join("")
            + (total > hits.length
               ? `<button type="button" class="nn-result nn-more list-group-item
                          list-group-item-action small text-muted">
                    All ${total} matches</button>`
               : "");
        }
        results.style.display = "block";
      }

      async function run() {
        const q = search.value.trim();
        if (q.length < 2) { hits = []; total = 0; hide(); return; }
        const mine = ++seq;
        const data = await fetch(`/notes/search?q=${encodeURIComponent(q)}`)
          .then((r) => r.json()).catch(() => null);
        // A slower earlier request must not overwrite a newer answer.
        if (mine !== seq || !data) return;
        hits = data.results || [];
        total = data.total || 0;
        active = -1;
        render();
      }

      function all() {
        const q = search.value.trim();
        window.location.href = q ? `/notes/?q=${encodeURIComponent(q)}` : "/notes/";
      }

      search.addEventListener("input", () => {
        clearTimeout(timer);
        timer = setTimeout(run, 200);
      });
      search.addEventListener("focus", () => { if (hits.length) render(); });
      search.addEventListener("keydown", (e) => {
        if (e.key === "Escape") { search.value = ""; hits = []; hide(); return; }
        if (e.key === "ArrowDown" || e.key === "ArrowUp") {
          if (!hits.length) return;
          e.preventDefault();
          active = (active + (e.key === "ArrowDown" ? 1 : -1) + hits.length) % hits.length;
          render();
          return;
        }
        if (e.key === "Enter") {
          e.preventDefault();
          // Standing on a hit opens it; otherwise the whole list as a page.
          if (active >= 0 && hits[active]) window.location.href = `/notes/${hits[active].id}`;
          else all();
        }
      });
      results.addEventListener("mousemove", (e) => {
        const row = e.target.closest("[data-i]");
        if (row) { active = parseInt(row.dataset.i, 10); render(); }
      });
      results.addEventListener("click", (e) => {
        if (e.target.closest(".nn-more")) { e.preventDefault(); all(); }
      });
      document.addEventListener("click", (e) => {
        if (!e.target.closest(".nn-search-wrap")) hide();
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

    // --- mirror: sync from here, without leaving the note ---
    const mirror = nav.querySelector(".nn-mirror");
    const syncBtn = mirror && mirror.querySelector(".nn-mirror-sync");
    if (syncBtn) {
      const when = mirror.querySelector(".nn-mirror-when");
      let running = false;
      syncBtn.addEventListener("click", async () => {
        if (running) return;
        running = true;
        let following = false;          // the worker has it; keep the spinner
        syncBtn.classList.add("is-running");
        const was = when.textContent;
        when.textContent = "Syncing…";
        try {
          const fd = new FormData();
          const csrf = document.querySelector('input[name="csrf_token"]');
          if (csrf) fd.append("csrf_token", csrf.value);
          const resp = await fetch(mirror.dataset.syncUrl, {
            method: "POST", body: fd, credentials: "same-origin",
            headers: { "X-Requested-With": "XMLHttpRequest" },
          });
          const data = await resp.json().catch(() => null);
          if (!resp.ok || !data) throw new Error("sync failed");
          const land = (state) => {
            // Back to saying WHEN, which is what this line is for the rest of
            // the time.
            when.textContent = state.when ? `Mirrored ${state.when}` : was;
            mirror.querySelector("a").title = state.status || "Notes mirror";
          };
          when.textContent = data.message;
          // The line is narrow, so a summary or an error gets cut off — the
          // whole of it has to be readable on hover.
          mirror.querySelector("a").title = data.message;
          if (data.ok && data.queued) {
            following = true;
            // Handed to a worker: what this response carries is the PREVIOUS
            // round. Keep saying "syncing" and wait for the real one, or the
            // line would snap back to the old time and only a reload would
            // ever correct it.
            when.textContent = "Syncing…";
            const stop = () => {
              running = false;
              syncBtn.classList.remove("is-running");
            };
            window.cairnMirrorFollow(mirror.dataset.stateUrl, data,
              (state) => { land(state); stop(); },
              () => {
                when.textContent = was;
                mirror.querySelector("a").title =
                  "Still syncing — see Settings → Notes on a share";
                stop();
              });
          } else if (data.ok) {
            // It ran in the request: this IS the answer. A failure stays put —
            // one that clears itself after a few seconds is one you can miss.
            setTimeout(() => land(data), 6000);
          }
        } catch (err) {
          when.textContent = "Sync failed";
          mirror.querySelector("a").title = "Sync failed — see Settings → Notes mirror";
        } finally {
          // Not when a worker took it: the button stays busy until the round
          // lands, so a second click cannot queue the same run again.
          if (!following) {
            running = false;
            syncBtn.classList.remove("is-running");
          }
        }
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
