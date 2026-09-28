/* Navbar notification bell.
 *
 * The list is rendered server-side; this keeps the read state in step with
 * what you actually did. Opening the dropdown marks NOTHING — the badge is
 * how many are left to go through, and a batch you work through one at a
 * time has to keep saying which ones are still waiting. A row is marked read
 * when you open it (the POST rides the navigation with `keepalive`), and
 * "Mark all read" is the deliberate sweep for the rest.
 */
(function () {
  const bell = document.getElementById("notifBell");
  if (!bell) return;

  const badge = document.getElementById("notifBadge");
  const markAllBtn = document.getElementById("notifMarkAll");
  const url = bell.dataset.markReadUrl;

  function csrf() {
    const el = bell.querySelector('input[name="csrf_token"]')
      || document.querySelector('input[name="csrf_token"]');
    return el ? el.value : "";
  }

  /* ids omitted → mark all read. `keepalive` so a click that navigates away
     still delivers the POST. */
  function post(ids) {
    const fd = new FormData();
    fd.append("csrf_token", csrf());
    (ids || []).forEach((id) => fd.append("ids", id));
    return fetch(url, {
      method: "POST",
      body: fd,
      keepalive: true,
      headers: { "X-Requested-With": "XMLHttpRequest" },
    }).catch(() => {});
  }

  function restyle(row) {
    row.classList.remove("notif-unread", "bg-body-tertiary");
    const icon = row.querySelector("i");
    if (icon) icon.classList.replace("text-primary", "text-muted");
    const text = row.querySelector(".fw-semibold");
    if (text) text.classList.remove("fw-semibold");
    const dot = row.querySelector(".notif-dot");
    if (dot) dot.hidden = true;
  }

  function setBadge(n) {
    if (!badge) return;
    badge.textContent = String(n);
    badge.hidden = n < 1;
  }

  function unreadRows() {
    return bell.querySelectorAll(".notif-item.notif-unread");
  }

  bell.addEventListener("click", (ev) => {
    const row = ev.target.closest(".notif-item");
    if (!row) return;
    // Opening in a new tab/window leaves this page (and this bell) alone.
    const newTab = ev.metaKey || ev.ctrlKey || ev.shiftKey || ev.button === 1;
    if (newTab) return;

    const href = row.getAttribute("href") || "#";
    if (!row.classList.contains("notif-unread")) {
      // A notification with nowhere to go stays put instead of jumping the page.
      if (href === "#") ev.preventDefault();
      return;
    }

    restyle(row);
    setBadge(unreadRows().length);
    // A row stands for every entry about its task; the older ones it hides
    // go read with it, or they would linger unread where nobody can see them.
    const done = post(row.dataset.notifIds.split(" ").filter(Boolean));
    ev.preventDefault();
    if (href === "#") return;
    // Follow the link only once the row is actually marked read: the target
    // page renders this same bell, and it must not come back still unread.
    const go = () => window.location.assign(href);
    done.then(go, go);
  });

  if (markAllBtn) {
    markAllBtn.addEventListener("click", () => {
      post();
      unreadRows().forEach(restyle);
      setBadge(0);
    });
  }
})();
