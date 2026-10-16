/* Tasks and notes open in a modal over the page, like Todoist's task dialog,
 * instead of replacing the page (and instead of yet another browser tab).
 *
 * The modal holds an <iframe> with the item's ordinary detail page. The page
 * notices it is framed (server side: ``item_embed`` in the context processor,
 * from ``?embed=1`` or ``Sec-Fetch-Dest: iframe``) and leaves out the navbar
 * and the sidebars, so everything the detail page already does (inspector,
 * subtasks, comments, the outliner of a note) works unchanged, scripts and
 * all. Nothing about the item is rendered twice.
 *
 * Two roles, one file:
 *   - the PAGE: a plain left click on a link to /tasks/<id> or /notes/<id>
 *     opens the modal. Modifier clicks, target=_blank and links some other
 *     handler already took (defaultPrevented) are left alone, so ⌘-click
 *     still opens a tab and the deep link is the same URL as before.
 *   - the FRAME: a link to another task or note opens in the same modal
 *     (a stack, with a back button), any other app link leaves the modal and
 *     opens as a page, Esc closes. A frame that lands anywhere but a task or
 *     note (e.g. the redirect after deleting) closes the modal and the page
 *     follows it.
 *
 * The address bar shows the open item (replaceState, no history entry): a
 * reload or a copied URL is the task's deep link, and Back stays clean.
 */
(function () {
  const ITEM_RE = /^\/(tasks|notes)\/([0-9a-f]{24})\/?$/i;
  const html = document.documentElement;
  const inFrame = window.parent !== window && html.classList.contains("item-embed");

  function itemPath(a) {
    if (!a || !a.href || a.target && a.target !== "_self") return null;
    if (a.hasAttribute("download")) return null;
    let u;
    try { u = new URL(a.href, location.href); } catch (e) { return null; }
    if (u.origin !== location.origin) return null;
    const m = u.pathname.match(ITEM_RE);
    return m ? { url: u, kind: m[1].toLowerCase() } : null;
  }

  function plainClick(ev) {
    return ev.button === 0 && !ev.metaKey && !ev.ctrlKey && !ev.shiftKey && !ev.altKey;
  }

  /* ------------------------------------------------------------ frame */
  if (inFrame) {
    let host = null;
    try { host = window.parent.CairnItemModal; } catch (e) { host = null; }
    if (!host) return;

    // Landed somewhere that is not a task or a note: that page belongs in
    // the window, not in the dialog.
    if (!ITEM_RE.test(location.pathname)) {
      host.leave(location.pathname + location.search + location.hash);
      return;
    }
    host.loaded(window);

    window.addEventListener("click", (ev) => {
      if (ev.defaultPrevented || !plainClick(ev)) return;
      const a = ev.target.closest && ev.target.closest("a[href]");
      if (!a) return;
      const raw = a.getAttribute("href") || "";
      if (!raw || raw.startsWith("#") || raw.startsWith("javascript:")) return;
      if (a.target && a.target !== "_self") return;
      let u;
      try { u = new URL(a.href, location.href); } catch (e) { return; }
      if (u.origin !== location.origin) return;
      if (u.pathname === location.pathname && u.hash) return;  // in-page anchor
      ev.preventDefault();
      if (ITEM_RE.test(u.pathname)) host.push(u.pathname + u.search + u.hash);
      else if (/\/(attachments|files|images|uploads)\//.test(u.pathname)) window.open(u.href, "_blank", "noopener");
      else host.leave(u.pathname + u.search + u.hash);
    });

    // Esc closes the dialog, unless something inside the page wants it: an
    // open menu, modal or panel (looked at in the capture phase, before its
    // own handler closes it), or a field being typed in (the outliner's vim
    // mode lives on Esc in a textarea).
    let busyOnEsc = false;
    window.addEventListener("keydown", (ev) => {
      if (ev.key === "Escape") {
        busyOnEsc = !!document.querySelector(".modal.show, .dropdown-menu.show, .offcanvas.show");
      }
    }, true);
    window.addEventListener("keydown", (ev) => {
      if (ev.key !== "Escape" || ev.defaultPrevented || busyOnEsc) return;
      const t = ev.target;
      if (t && (t.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName))) return;
      host.close();
    });

    // Unsaved inspector edits: asked about before the dialog closes.
    window.cairnItemDirty = false;
    document.addEventListener("input", (ev) => {
      if (ev.target.closest && ev.target.closest("form[data-inspector-form]")) window.cairnItemDirty = true;
    });
    document.addEventListener("change", (ev) => {
      if (ev.target.closest && ev.target.closest("form[data-inspector-form]")) window.cairnItemDirty = true;
    });
    document.addEventListener("submit", (ev) => {
      if (ev.target.matches && ev.target.matches("form[data-inspector-form]")) window.cairnItemDirty = false;
    }, true);
    return;
  }

  /* ------------------------------------------------------------- page */
  if (html.classList.contains("item-embed")) return;  // embed opened as a page
  if (!window.bootstrap) return;

  let modalEl = null, frame = null, stack = [], pageUrl = null, opener = null;

  function build() {
    if (modalEl) return;
    modalEl = document.createElement("div");
    modalEl.className = "modal fade item-modal";
    modalEl.id = "item-modal";
    modalEl.tabIndex = -1;
    modalEl.setAttribute("aria-hidden", "true");
    modalEl.setAttribute("aria-labelledby", "item-modal-title");
    modalEl.innerHTML =
      '<div class="modal-dialog modal-xl modal-fullscreen-lg-down">' +
      '<div class="modal-content">' +
      '<div class="item-modal-bar">' +
      '<button type="button" class="btn btn-sm btn-link item-modal-back" hidden title="Back to the previous item">' +
      '<i class="bi bi-arrow-left"></i></button>' +
      '<span class="item-modal-kind"></span>' +
      '<h2 class="item-modal-title" id="item-modal-title"></h2>' +
      '<a class="btn btn-sm btn-link item-modal-full" title="Open as a full page"><i class="bi bi-arrows-angle-expand"></i></a>' +
      '<button type="button" class="btn-close" aria-label="Close (Esc)" title="Close (Esc)"></button>' +
      "</div>" +
      '<iframe class="item-modal-frame" title="Item"></iframe>' +
      "</div></div>";
    document.body.appendChild(modalEl);
    frame = modalEl.querySelector("iframe");
    modalEl.querySelector(".btn-close").addEventListener("click", close);
    modalEl.querySelector(".item-modal-back").addEventListener("click", back);
    // Backdrop click and Esc go through hide.bs.modal: ask about unsaved edits
    // there, once, for every way of closing.
    modalEl.addEventListener("hide.bs.modal", (ev) => {
      if (!confirmDiscard()) ev.preventDefault();
    });
    modalEl.addEventListener("hidden.bs.modal", () => {
      if (pageUrl) history.replaceState(history.state, "", pageUrl);
      pageUrl = null;
      stack = [];
      frame.removeAttribute("src");
      frame.src = "about:blank";
      // What was changed in the dialog shows in the list behind it.
      document.dispatchEvent(new Event("bktodo:refresh-needed"));
      if (opener && document.contains(opener)) opener.focus();
      opener = null;
    });
  }

  function confirmDiscard() {
    let dirty = false;
    try { dirty = !!(frame && frame.contentWindow && frame.contentWindow.cairnItemDirty); } catch (e) {}
    if (!dirty) return true;
    if (!confirm("Discard the unsaved changes in the inspector?")) return false;
    try { frame.contentWindow.cairnItemDirty = false; } catch (e) {}
    return true;
  }

  function withEmbed(path) {
    const u = new URL(path, location.href);
    u.searchParams.set("embed", "1");
    return u.pathname + u.search + u.hash;
  }

  function show(path) {
    frame.src = withEmbed(path);
    modalEl.querySelector(".item-modal-back").hidden = stack.length < 2;
    const full = modalEl.querySelector(".item-modal-full");
    full.href = path;
    const kind = (path.match(ITEM_RE) || [])[1] || "";
    modalEl.querySelector(".item-modal-kind").innerHTML = kind === "notes"
      ? '<i class="bi bi-journal-text"></i>' : '<i class="bi bi-check2-square"></i>';
    modalEl.querySelector(".item-modal-title").textContent = "";
    history.replaceState(history.state, "", path.split("?")[0]);
  }

  function open(path, trigger) {
    build();
    if (!pageUrl) pageUrl = location.pathname + location.search + location.hash;
    opener = trigger || document.activeElement;
    stack = [path];
    show(path);
    bootstrap.Modal.getOrCreateInstance(modalEl).show();
  }

  function push(path) {
    if (!confirmDiscard()) return;
    stack.push(path);
    show(path);
  }

  function back() {
    if (stack.length < 2 || !confirmDiscard()) return;
    stack.pop();
    show(stack[stack.length - 1]);
  }

  function close() {
    if (modalEl) bootstrap.Modal.getOrCreateInstance(modalEl).hide();
  }

  // The frame ended up on a page that is not a task or note, or the user
  // followed a link to one: show it as the page.
  function leave(path) {
    const target = new URL(path, location.href);
    target.searchParams.delete("embed");
    const here = pageUrl || (location.pathname + location.search);
    if (frame && frame.contentWindow) {
      try { frame.contentWindow.cairnItemDirty = false; } catch (e) {}
    }
    if (target.pathname + target.search === here.split("#")[0]) { close(); return; }
    if (pageUrl) history.replaceState(history.state, "", pageUrl);
    pageUrl = null;
    location.href = target.pathname + target.search + target.hash;
  }

  function loaded(win) {
    const title = (win.document.title || "").replace(/\s*·\s*Cairn\s*$/, "");
    modalEl.querySelector(".item-modal-title").textContent = title;
    // A redirect inside the frame (e.g. the full page of a parent task) keeps
    // the address bar honest.
    const p = win.location.pathname;
    if (ITEM_RE.test(p)) {
      history.replaceState(history.state, "", p);
      modalEl.querySelector(".item-modal-full").href = p;
    }
    // Keys go to the item, not to the page behind the dialog.
    try { win.focus(); } catch (e) {}
  }

  window.CairnItemModal = { open, push, back, close, leave, loaded };

  // Bubble phase on window: runs after every handler on document, so a link
  // another script already handles (quick edit, lightbox, ...) is left alone.
  window.addEventListener("click", (ev) => {
    if (ev.defaultPrevented || !plainClick(ev)) return;
    const a = ev.target.closest && ev.target.closest("a[href]");
    const hit = itemPath(a);
    if (!hit) return;
    // The notes pages navigate between notes themselves.
    if (hit.kind === "notes" && location.pathname.startsWith("/notes")) return;
    // Phones: the dialog would be the whole screen anyway, and a page has
    // the browser's own Back. Keep the plain navigation there.
    if (window.matchMedia && window.matchMedia("(max-width: 575.98px)").matches) return;
    ev.preventDefault();
    open(hit.url.pathname + hit.url.search + hit.url.hash, a);
  });
})();
