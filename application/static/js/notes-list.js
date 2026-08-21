/* Notes results column: what the tree on the left cannot show.
 *
 * The tree IS the navigation — folders, pages, and the blocks inside the open
 * one. So this column is not permanently there; it opens when there is a
 * full-text hit or a tag to show, and shuts again when the search is cleared.
 * The button that toggles it by hand still works, for browsing and for sorting.
 *
 * The search field and the tag picker live in the NAVIGATOR (.nn-search /
 * .nn-tag), not in here: this column is re-fetched as a server-rendered
 * partial and swapped wholesale on every keystroke, so a field inside it lost
 * its own focus and had to have it put back by hand.
 */
(function () {
  const LIST_KEY = "cairn.notes.listOff";
  const INSPECTOR_KEY = "cairn.notes.inspectorOff";

  function wrap() { return document.querySelector(".notes-wrap"); }
  function col() { return document.getElementById("notes-list-col"); }

  // ---- column visibility ----------------------------------------------
  function stored(key, fallback) {
    const v = localStorage.getItem(key);
    return v === null ? fallback : v === "1";
  }
  /** Is there a search or a tag on? Then the column has something only it can
   *  show, and it opens whatever the stored preference says. */
  function filtering() {
    const c = col();
    return !!(c && (c.dataset.q || c.dataset.tag));
  }

  function applyColumns() {
    const w = wrap();
    if (!w) return;
    // Shut unless asked for, or unless a filter is on — the tree is the
    // navigation, and two lists of the same notes side by side is one too many.
    // The inspector still folds itself away by viewport: the note body needs
    // ~600px to read comfortably.
    const listOff = !filtering() && stored(LIST_KEY, true);
    const inspOff = stored(INSPECTOR_KEY, window.innerWidth < 1500);
    w.classList.toggle("list-off", listOff);
    w.classList.toggle("inspector-off", inspOff && !!w.querySelector(".notes-inspector"));
    document.querySelectorAll("[data-toggle-column]").forEach((btn) => {
      const off = btn.dataset.toggleColumn === "list" ? listOff : inspOff;
      btn.classList.toggle("active", !off);
      btn.setAttribute("aria-pressed", off ? "false" : "true");
    });
  }

  document.addEventListener("click", (e) => {
    const btn = e.target.closest("[data-toggle-column]");
    if (!btn) return;
    const key = btn.dataset.toggleColumn === "list" ? LIST_KEY : INSPECTOR_KEY;
    const w = wrap();
    const cls = btn.dataset.toggleColumn === "list" ? "list-off" : "inspector-off";
    const nowOff = !w.classList.contains(cls);
    localStorage.setItem(key, nowOff ? "1" : "0");
    applyColumns();
  });

  // ---- filtering -------------------------------------------------------
  function params(overrides) {
    const c = col();
    const p = new URLSearchParams();
    const cur = {
      folder: c ? c.dataset.folder : "",
      tag: c ? c.dataset.tag : "",
      q: c ? c.dataset.q : "",
      sort: c ? c.dataset.sort : "updated",
    };
    Object.assign(cur, overrides || {});
    for (const k of ["folder", "tag", "q", "sort"]) {
      if (cur[k] && !(k === "sort" && cur[k] === "updated")) p.set(k, cur[k]);
    }
    return p;
  }

  let pending = null;
  async function refresh(overrides) {
    const c = col();
    if (!c) return;
    const p = params(overrides);
    c.classList.add("opacity-50");
    if (pending) pending.abort();
    pending = new AbortController();
    try {
      const resp = await fetch(`/notes/list?${p.toString()}`, {
        signal: pending.signal,
        headers: { "X-Requested-With": "XMLHttpRequest" },
      });
      if (!resp.ok) throw new Error(resp.status);
      const html = await resp.text();
      const holder = document.createElement("div");
      holder.innerHTML = html;
      const fresh = holder.firstElementChild;
      c.replaceWith(fresh);
      // Keep the open note highlighted — the partial doesn't know about it.
      const open = document.getElementById("note-detail");
      if (open) {
        const row = fresh.querySelector(`a[href*="${open.dataset.noteId}"]`);
        if (row) row.classList.add("active");
      }
      // Reflect the filter in the URL so a reload (or Back) keeps the view.
      const url = p.toString() ? `${location.pathname}?${p}` : location.pathname;
      history.replaceState(null, "", url);
    } catch (err) {
      if (err.name !== "AbortError") c.classList.remove("opacity-50");
    }
  }

  /** Refresh, then open or shut the column according to what came back. */
  async function search(overrides) {
    await refresh(overrides);
    applyColumns();
  }

  function clearSearch() {
    const q = document.querySelector(".nn-search");
    const tag = document.querySelector(".nn-tag");
    if (q) q.value = "";
    if (tag) tag.value = "";
  }

  let searchTimer = null;
  document.addEventListener("input", (e) => {
    if (!e.target.classList.contains("nn-search")) return;
    clearTimeout(searchTimer);
    const value = e.target.value.trim();
    searchTimer = setTimeout(() => search({ q: value }), 250);
  });
  document.addEventListener("change", (e) => {
    if (e.target.classList.contains("nn-tag")) search({ tag: e.target.value });
  });
  document.addEventListener("click", (e) => {
    const sort = e.target.closest(".nl-sort");
    if (sort) { refresh({ sort: sort.dataset.sort }); return; }
    if (e.target.closest(".nl-clear")) {
      clearSearch();
      search({ q: "", tag: "", folder: "" });
    }
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && e.target.classList.contains("nn-search")
        && e.target.value) {
      e.target.value = "";
      e.target.dispatchEvent(new Event("input", { bubbles: true }));
    }
  });

  if (document.readyState !== "loading") applyColumns();
  else document.addEventListener("DOMContentLoaded", applyColumns);
})();
