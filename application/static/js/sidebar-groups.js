/* Foldable groups in the left sidebar.
 *
 * Each group header is a real <button> (Tab reaches it, Enter and Space fold
 * it). Folding flips the group at once and posts the new state to
 * /settings/sidebar-group, which stores it on the user: the next page, in any
 * browser, is rendered folded by the server, so there is no flash and no
 * localStorage. A failed post leaves the fold in place for this page only.
 */
(function () {
  function csrf() {
    const el = document.querySelector('input[name="csrf_token"]');
    return el ? el.value : "";
  }

  function setState(group, collapsed) {
    const btn = group.querySelector(".nav-group-toggle");
    const body = group.querySelector(".nav-group-body");
    group.classList.toggle("is-collapsed", collapsed);
    if (btn) btn.setAttribute("aria-expanded", collapsed ? "false" : "true");
    if (body) body.hidden = collapsed;
  }

  document.addEventListener("click", (ev) => {
    const btn = ev.target.closest(".nav-group-toggle");
    if (!btn) return;
    const group = btn.closest("[data-nav-group]");
    if (!group) return;
    ev.preventDefault();
    const collapsed = btn.getAttribute("aria-expanded") === "true";
    setState(group, collapsed);
    const fd = new FormData();
    fd.append("csrf_token", csrf());
    fd.append("group", group.dataset.navGroup);
    fd.append("collapsed", collapsed ? "1" : "0");
    fetch("/settings/sidebar-group", {
      method: "POST", body: fd, credentials: "same-origin",
      headers: { "X-Requested-With": "XMLHttpRequest" },
    }).catch(() => { /* offline: the fold holds for this page */ });
  });
})();
