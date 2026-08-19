/* Notes middle column: search / sort / tag filter, plus the column toggles.
 *
 * The list is re-fetched as a server-rendered partial (/notes/list) and swapped
 * into #notes-list-col, so typing a search never reloads the page — the open
 * note (and its unsaved editor state) survives. Everything is delegated on
 * document because the markup it wires up is replaced wholesale on every
 * filter change.
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
  function applyColumns() {
    const w = wrap();
    if (!w) return;
    // Defaults by viewport: the note body needs ~600px to read comfortably,
    // so a narrow window folds the side columns away until asked otherwise.
    const listOff = stored(LIST_KEY, window.innerWidth < 1100);
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
  async function refresh(overrides, focusSelector) {
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
      if (focusSelector) {
        const el = fresh.querySelector(focusSelector);
        if (el) { el.focus(); el.selectionStart = el.selectionEnd = el.value.length; }
      }
    } catch (err) {
      if (err.name !== "AbortError") c.classList.remove("opacity-50");
    }
  }

  let searchTimer = null;
  document.addEventListener("input", (e) => {
    if (!e.target.classList.contains("nl-search")) return;
    clearTimeout(searchTimer);
    const value = e.target.value.trim();
    searchTimer = setTimeout(() => refresh({ q: value }, ".nl-search"), 250);
  });
  document.addEventListener("change", (e) => {
    if (e.target.classList.contains("nl-tag")) refresh({ tag: e.target.value });
  });
  document.addEventListener("click", (e) => {
    const sort = e.target.closest(".nl-sort");
    if (sort) { refresh({ sort: sort.dataset.sort }); return; }
    if (e.target.closest(".nl-clear")) refresh({ q: "", tag: "", folder: "" });
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && e.target.classList.contains("nl-search")
        && e.target.value) {
      e.target.value = "";
      refresh({ q: "" }, ".nl-search");
    }
  });

  if (document.readyState !== "loading") applyColumns();
  else document.addEventListener("DOMContentLoaded", applyColumns);
})();
