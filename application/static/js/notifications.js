/* Navbar notification bell.
 *
 * The list is rendered server-side; this keeps the read state in step with
 * what you actually did. Opening the dropdown marks NOTHING — the badge is
 * how many are left to go through, and a batch you work through one at a
 * time has to keep saying which ones are still waiting. A row is marked read
 * when you open it (the POST rides the navigation with `keepalive`), and
 * "Mark all read" is the deliberate sweep for the rest.
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
    const done = marked(post(row.dataset.notifIds.split(" ").filter(Boolean)));
    ev.preventDefault();
    if (href === "#") return;
    // Follow the link only once the row is actually marked read: the target
    // page renders this same bell, and it must not come back still unread.
    const go = () => window.location.assign(href);
    done.then(go, go);
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
