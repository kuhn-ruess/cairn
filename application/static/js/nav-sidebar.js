/* Hide or show the left navigation on a desktop, for room (the button left
   of the brand, or ``b``). Below 992px the sidebar is the drawer
   (nav-drawer.js) and the class set here has no effect there.

   Same mechanism as the project tree's collapse (project-sidebar.js): one
   class on <html>, kept in localStorage and put back by the head script in
   base.html before anything paints, so a hidden sidebar never flashes in.
   Per browser on purpose: a laptop screen wants the room, the big monitor
   on the desk does not. */
(function () {
  "use strict";
  const KEY = "cairn.navSidebar.collapsed";
  const CLASS = "cairn-nav-collapsed";
  const root = document.documentElement;
  const button = document.querySelector("[data-nav-sidebar-toggle]");

  function sync() {
    if (!button) return;
    const hidden = root.classList.contains(CLASS);
    const label = hidden ? "Show navigation" : "Hide navigation";
    button.setAttribute("aria-expanded", hidden ? "false" : "true");
    button.setAttribute("aria-label", label);
    button.title = label + " (b)";
  }

  function toggle() {
    const hidden = !root.classList.contains(CLASS);
    root.classList.toggle(CLASS, hidden);
    try { localStorage.setItem(KEY, hidden ? "1" : "0"); } catch (e) { /* blocked */ }
    sync();
  }

  if (button) button.addEventListener("click", toggle);
  sync();
  window.CairnNavSidebar = { toggle };
})();
