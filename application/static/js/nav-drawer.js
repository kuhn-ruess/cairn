/* The left sidebar as a slide-out drawer on phones and tablets (below 992px,
   where the navbar shows the hamburger). Above that the sidebar is the plain
   column it always was and this file does nothing.

   State is one class on <html> (``nav-drawer-open``), so the CSS owns the
   look. While the drawer is shut on a narrow screen the sidebar is ``inert``:
   off screen must also mean out of the tab order and away from screen
   readers. The group folding inside it is sidebar-groups.js, unchanged. */
(function () {
  "use strict";
  const drawer = document.getElementById("app-nav-drawer");
  const toggle = document.querySelector("[data-nav-drawer-toggle]");
  const backdrop = document.querySelector(".nav-drawer-backdrop");
  if (!drawer || !toggle) return;

  const root = document.documentElement;
  const narrow = window.matchMedia("(max-width: 991.98px)");

  const isOpen = () => root.classList.contains("nav-drawer-open");

  function sync() {
    const open = isOpen();
    toggle.setAttribute("aria-expanded", open ? "true" : "false");
    toggle.setAttribute("aria-label", open ? "Close navigation" : "Open navigation");
    if (narrow.matches && !open) drawer.setAttribute("inert", "");
    else drawer.removeAttribute("inert");
    if (backdrop) backdrop.hidden = !open;
  }

  function open() {
    if (backdrop) backdrop.hidden = false;
    // One frame between un-hiding the backdrop and the class, so it fades.
    requestAnimationFrame(() => {
      root.classList.add("nav-drawer-open");
      sync();
      // Focus the drawer itself (no ring on a link nobody chose), and bring
      // the current page's entry into view.
      drawer.focus({ preventScroll: true });
      const active = drawer.querySelector(".nav-link.active");
      if (active && active.scrollIntoView) active.scrollIntoView({ block: "nearest" });
    });
  }

  function close(returnFocus) {
    if (!isOpen()) return;
    root.classList.remove("nav-drawer-open");
    sync();
    if (returnFocus) toggle.focus({ preventScroll: true });
  }

  toggle.addEventListener("click", () => (isOpen() ? close(true) : open()));
  document.querySelectorAll("[data-nav-drawer-close]").forEach((el) =>
    el.addEventListener("click", () => close(true)));
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && isOpen()) { e.preventDefault(); close(true); }
  });
  // Following a link leaves the page anyway; close first so a Back from the
  // next page (bfcache) does not come back to an open drawer.
  drawer.addEventListener("click", (e) => {
    const a = e.target.closest("a[href]");
    if (a && !e.defaultPrevented) close(false);
  });
  // The quick add / log time buttons open a dialog over the page.
  drawer.addEventListener("click", (e) => {
    if (e.target.closest("[data-bs-toggle='modal']")) close(false);
  });
  window.addEventListener("pageshow", () => close(false));
  narrow.addEventListener("change", () => { root.classList.remove("nav-drawer-open"); sync(); });
  sync();
})();
