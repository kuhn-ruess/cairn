/* Collapsible detail sections (Note / Subtasks / Comments / …).
 *
 * A button carrying [data-collapse-key] toggles the ".section-collapsible" body
 * of its enclosing [data-collapsible] section. The open/closed choice is kept in
 * localStorage under that key and re-applied whenever a partial (e.g. the
 * subtasks widget) is swapped back in via AJAX. Document-level delegation, so it
 * works on the inline task-detail page AND inside the off-canvas inspector.
 */
(function () {
  function stored(key) {
    try { return localStorage.getItem(key) === "1"; } catch (e) { return false; }
  }
  function bodyFor(btn) {
    const sec = btn.closest("[data-collapsible]");
    return sec ? sec.querySelector(".section-collapsible") : null;
  }
  function paint(btn) {
    const body = bodyFor(btn);
    if (!body) return;
    const collapsed = stored(btn.dataset.collapseKey);
    body.classList.toggle("d-none", collapsed);
    const icon = btn.querySelector("i");
    if (icon) icon.className = collapsed ? "bi bi-chevron-right" : "bi bi-chevron-down";
    btn.setAttribute("aria-expanded", collapsed ? "false" : "true");
  }
  function paintAll(scope) {
    (scope || document).querySelectorAll("[data-collapse-key]").forEach(paint);
  }

  document.addEventListener("click", (ev) => {
    const btn = ev.target.closest("[data-collapse-key]");
    if (!btn) return;
    ev.preventDefault();
    const key = btn.dataset.collapseKey;
    try { localStorage.setItem(key, stored(key) ? "0" : "1"); } catch (e) {}
    paint(btn);
  });

  paintAll();

  // Re-apply the remembered state to partials that get re-rendered (subtasks).
  // We only react to added nodes, and only ever toggle a class — which is an
  // attribute change, not childList — so this never feeds back into itself.
  new MutationObserver((muts) => {
    for (const m of muts) {
      for (const n of m.addedNodes) {
        if (n.nodeType !== 1) continue;
        if (n.matches && n.matches("[data-collapse-key]")) paint(n);
        if (n.querySelectorAll) paintAll(n);
      }
    }
  }).observe(document.body, { childList: true, subtree: true });
})();
