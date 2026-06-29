/* Searchable wrapper for a native <select>.
 * Mark with data-searchable-select; we add a search input that filters
 * the options on input. Keeps native form submission (the <select> is
 * still the source of truth).
 */
(function () {
  function wire(sel) {
    if (sel.dataset.searchableWired === "1") return;
    sel.dataset.searchableWired = "1";

    const wrap = document.createElement("div");
    wrap.className = "searchable-select-wrap";
    sel.parentNode.insertBefore(wrap, sel);
    wrap.appendChild(sel);

    const search = document.createElement("input");
    search.type = "search";
    search.className = "form-control form-control-sm mb-1 searchable-select-input";
    search.placeholder = sel.dataset.searchablePlaceholder || "Search…";
    wrap.insertBefore(search, sel);

    const opts = Array.from(sel.options);

    function filter() {
      const q = search.value.trim().toLowerCase();
      let firstVisible = null;
      for (const o of opts) {
        const txt = (o.textContent || "").toLowerCase();
        const matches = !q || txt.includes(q);
        o.hidden = !matches;
        // Native <select> ignores .hidden in some browsers — fall back
        // to display:none on the option element.
        o.style.display = matches ? "" : "none";
        if (matches && !firstVisible) firstVisible = o;
      }
      if (q && firstVisible && !sel.multiple) {
        // Highlight the first match without forcing it (don't commit
        // until the user clicks or presses Enter).
        sel.value = firstVisible.value;
      }
    }

    search.addEventListener("input", filter);
    search.addEventListener("keydown", (e) => {
      if (e.key === "Enter") {
        e.preventDefault();
        filter();
      }
    });

    if (sel.multiple) {
      // A native <select multiple> replaces the whole selection on a plain
      // click — you'd need Cmd/Ctrl-click to add. That makes picking a second
      // member silently drop the first. Make each click TOGGLE its option
      // instead, leaving the others intact.
      sel.addEventListener("mousedown", (e) => {
        const opt = e.target.closest("option");
        if (!opt || opt.disabled) return;
        e.preventDefault();
        opt.selected = !opt.selected;
        sel.dispatchEvent(new Event("change", { bubbles: true }));
        sel.focus();
      });
    }
  }

  function init() {
    document.querySelectorAll("select[data-searchable-select]").forEach(wire);
  }

  if (document.readyState !== "loading") init();
  else document.addEventListener("DOMContentLoaded", init);

  // The inspector loads forms via fetch — re-run on each open.
  document.addEventListener("inspector:loaded", init);
})();
