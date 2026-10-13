/* Navbar notification bell.
 *
 * The list is rendered server-side; this keeps the read state in step with
 * what you actually did. Opening the dropdown marks NOTHING — the badge is
 * how many are left to go through, and a batch you work through one at a
 * time has to keep saying which ones are still waiting. A row is marked read
 * when you open it, by whatever means: its link goes through
 * /notifications/<id>/open, which marks it read before redirecting. "Mark all
 * read" is the deliberate sweep for the rest.
 *
 * It also keeps itself current: every 30 s (and at once when the tab comes
 * back into view) it asks /notifications/bell whether anything changed since
 * the fingerprint it holds. Nothing new is a 304 and costs nothing; something
 * new updates the badge right away, while the list itself is swapped only
 * when the dropdown is closed — a row must not move under the pointer. A
 * hidden tab doesn't poll at all.
 */
(function () {
  const bell = document.getElementById("notifBell");
  if (!bell) return;

  const badge = document.getElementById("notifBadge");
  const menu = document.getElementById("notifMenu");
  const url = bell.dataset.markReadUrl;
  const bellUrl = bell.dataset.bellUrl;
  const POLL_MS = 30000;
  let etag = bell.dataset.etag || "";
  let pendingHtml = null;   // a fresh list held back while the dropdown is open
  let gen = 0;              // bumped by a local mark-read: older answers are stale

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

  /* A row with a link points at /notifications/<id>/open, which marks it
     read on the server and redirects; that covers every way of opening it,
     the context menu's "Open in new tab" included. Here the row only has to
     LOOK read at once, in this page's bell. A row without a link has no
     such address, so its click posts the mark-read itself. */
  function opened(row) {
    if (!row.classList.contains("notif-unread")) return;
    restyle(row);
    setBadge(unreadRows().length);
    gen += 1;           // a list fetched before this still shows it unread
    pendingHtml = null;
  }

  bell.addEventListener("click", (ev) => {
    // Delegated, because a poll replaces the menu body this button lives in.
    if (ev.target.closest("#notifMarkAll")) {
      marked(post());
      unreadRows().forEach(restyle);
      setBadge(0);
      return;
    }
    const row = ev.target.closest(".notif-item");
    if (!row) return;
    if ((row.getAttribute("href") || "#") !== "#") {
      opened(row);      // the browser follows the link, in this tab or a new one
      // A new tab leaves this page here: ask once the server has seen it.
      if (ev.metaKey || ev.ctrlKey || ev.shiftKey) setTimeout(poll, 1500);
      return;
    }
    // A notification with nowhere to go stays put instead of jumping the page.
    ev.preventDefault();
    if (!row.classList.contains("notif-unread")) return;
    restyle(row);
    setBadge(unreadRows().length);
    // A row stands for every entry about its task; the older ones it hides
    // go read with it, or they would linger unread where nobody can see them.
    marked(post(row.dataset.notifIds.split(" ").filter(Boolean)));
  });

  // A middle click is no "click" event; it opens the link in a new tab.
  bell.addEventListener("auxclick", (ev) => {
    if (ev.button !== 1) return;
    const row = ev.target.closest(".notif-item");
    if (!row || (row.getAttribute("href") || "#") === "#") return;
    opened(row);
    setTimeout(poll, 1500);
  });

  /* What the page just marked read makes any list fetched before it stale —
     drop that, and ask again once the server has the new state. */
  function marked(done) {
    gen += 1;
    pendingHtml = null;
    done.then(poll);
    return done;
  }

  function isOpen() {
    return menu && menu.classList.contains("show");
  }

  function swapIn() {
    if (pendingHtml === null || !menu) return;
    menu.innerHTML = pendingHtml;
    pendingHtml = null;
  }

  let inFlight = false;
  function poll() {
    if (!bellUrl || inFlight || document.hidden) return;
    inFlight = true;
    const asked = gen;
    fetch(bellUrl, {
      headers: { "If-None-Match": '"' + etag + '"', "Accept": "application/json" },
      cache: "no-store",
      credentials: "same-origin",
    })
      .then((r) => (r.status === 200 ? r.json() : null))
      .then((data) => {
        if (!data || typeof data.html !== "string" || asked !== gen) return;
        etag = data.etag;
        setBadge(data.unread);
        pendingHtml = data.html;
        if (!isOpen()) swapIn();
      })
      .catch(() => {})   // offline, logged out (login page isn't JSON): try later
      .finally(() => { inFlight = false; });
  }

  // A list that arrived while the dropdown was open goes in once it closes.
  bell.addEventListener("hidden.bs.dropdown", swapIn);

  setInterval(poll, POLL_MS);
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) poll();
  });
})();
