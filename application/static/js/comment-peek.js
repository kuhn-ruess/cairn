/* Read a task's comments straight from a list — hover the comment chip.
 *
 * Why it looks the way it does:
 *  - The panel lives in <body>, NOT inside the chip: a kanban column and a
 *    task list both scroll/clip their own contents, and a panel nested in the
 *    row would be cut off. Position is computed per open (fixed coordinates).
 *  - Opening is delayed and closing is forgiving, because the chip sits in a
 *    dense row: a pointer crossing it on the way somewhere else must not fire,
 *    and the trip from the chip ONTO the panel crosses a gap that would
 *    otherwise count as "mouse out". The panel cancels the pending close on
 *    mouseenter, which is what makes a scrollable panel usable at all.
 *  - Content is fetched on OPEN and kept per task for the life of the page,
 *    so hovering the same chip repeatedly is one request, and rendering a list
 *    of a hundred rows still costs what it did before this existed.
 *  - Hover is mouse-only, so the chip is also focusable (Tab opens it) and
 *    click/tap PINS it open — that is the touch and keyboard path. A pinned
 *    panel ignores the pointer leaving; Escape or a click outside closes it.
 *  - Everything is delegated on document and this file is loaded from
 *    base.html, so the <main> swap after an inspector save can't orphan it.
 */
(function () {
  "use strict";

  var OPEN_DELAY = 200;   // ms a pointer must rest on the chip
  var CLOSE_DELAY = 250;  // ms of grace to reach the panel (or come back)
  var GAP = 6;            // px between chip and panel
  var MARGIN = 8;         // px kept free at the viewport edges
  var MAX_H = 360;        // px, panel is scrollable beyond this

  var cache = new Map();     // url -> html
  var inflight = new Map();  // url -> promise (a second hover joins the first)
  var panel = null, inner = null;
  var current = null;        // the chip the open panel belongs to
  var pinned = false;
  var openTimer = null, closeTimer = null, seq = 0, rafPending = false;

  function closest(node, sel) {
    return node && node.closest ? node.closest(sel) : null;
  }

  function ensurePanel() {
    if (panel) return panel;
    panel = document.createElement("div");
    panel.className = "comment-peek";
    panel.id = "comment-peek";
    panel.setAttribute("role", "tooltip");
    panel.hidden = true;
    inner = document.createElement("div");
    inner.className = "comment-peek-inner";
    panel.appendChild(inner);
    panel.addEventListener("mouseenter", cancelClose);
    panel.addEventListener("mouseleave", function () { if (!pinned) scheduleClose(); });
    document.body.appendChild(panel);
    return panel;
  }

  function cancelOpen() { if (openTimer) { clearTimeout(openTimer); openTimer = null; } }
  function cancelClose() { if (closeTimer) { clearTimeout(closeTimer); closeTimer = null; } }

  function scheduleOpen(trigger) {
    cancelOpen();
    openTimer = setTimeout(function () { openTimer = null; open(trigger); }, OPEN_DELAY);
  }

  function scheduleClose() {
    cancelClose();
    closeTimer = setTimeout(function () { closeTimer = null; close(); }, CLOSE_DELAY);
  }

  function load(url) {
    if (cache.has(url)) return Promise.resolve(cache.get(url));
    var p = inflight.get(url);
    if (p) return p;
    p = fetch(url, {
      credentials: "same-origin",
      headers: { "X-Partial": "1", "X-Requested-With": "XMLHttpRequest" },
    }).then(function (r) {
      if (!r.ok) throw new Error("comment peek: HTTP " + r.status);
      return r.text();
    }).then(function (html) {
      cache.set(url, html);
      inflight.delete(url);
      return html;
    }).catch(function (err) {
      inflight.delete(url);
      throw err;
    });
    inflight.set(url, p);
    return p;
  }

  function open(trigger) {
    if (!trigger || !trigger.dataset.commentPeek) return;
    ensurePanel();
    if (current && current !== trigger) current.setAttribute("aria-expanded", "false");
    current = trigger;
    trigger.setAttribute("aria-expanded", "true");
    trigger.setAttribute("aria-describedby", "comment-peek");
    panel.hidden = false;
    var url = trigger.dataset.commentPeek;
    var mine = ++seq;
    if (cache.has(url)) {
      inner.innerHTML = cache.get(url);
      panel.scrollTop = 0;
      position();
      return;
    }
    inner.innerHTML = '<div class="comment-peek-status">Loading comments…</div>';
    panel.scrollTop = 0;
    position();
    load(url).then(function (html) {
      if (mine !== seq || current !== trigger) return;  // stale: panel moved on
      inner.innerHTML = html;
      panel.scrollTop = 0;
      position();
    }).catch(function () {
      if (mine !== seq || current !== trigger) return;
      inner.innerHTML = '<div class="comment-peek-status">Comments could not be loaded.</div>';
      position();
    });
  }

  function close() {
    cancelOpen();
    cancelClose();
    pinned = false;
    seq++;
    if (current) {
      current.setAttribute("aria-expanded", "false");
      current.removeAttribute("aria-describedby");
    }
    current = null;
    if (panel) {
      panel.hidden = true;
      panel.style.maxHeight = "";
    }
  }

  function position() {
    if (!current || !panel || panel.hidden) return;
    var r = current.getBoundingClientRect();
    var vw = document.documentElement.clientWidth;
    var vh = document.documentElement.clientHeight;
    if (r.bottom < 0 || r.top > vh) { close(); return; }  // scrolled out of sight
    var below = vh - r.bottom - GAP - MARGIN;
    var above = r.top - GAP - MARGIN;
    var useAbove = below < 160 && above > below;
    var space = Math.max(120, useAbove ? above : below);
    panel.style.maxHeight = Math.min(MAX_H, space) + "px";
    var w = panel.offsetWidth;
    var left = Math.max(MARGIN, Math.min(r.left, vw - w - MARGIN));
    panel.style.left = left + "px";
    if (useAbove) {
      panel.style.top = Math.max(MARGIN, r.top - GAP - panel.offsetHeight) + "px";
    } else {
      panel.style.top = (r.bottom + GAP) + "px";
    }
  }

  // -- pointer ---------------------------------------------------------

  document.addEventListener("mouseover", function (e) {
    var t = closest(e.target, "[data-comment-peek]");
    if (!t) return;
    cancelClose();
    if (t === current) return;
    scheduleOpen(t);
  });

  document.addEventListener("mouseout", function (e) {
    var t = closest(e.target, "[data-comment-peek]");
    if (!t) return;
    var to = e.relatedTarget;
    if (to && (t.contains(to) || (panel && panel.contains(to)))) return;
    cancelOpen();
    if (!pinned) scheduleClose();
  });

  // Capture, so the chip's click never reaches a row handler underneath.
  document.addEventListener("click", function (e) {
    var t = closest(e.target, "[data-comment-peek]");
    if (t) {
      e.preventDefault();
      e.stopPropagation();
      cancelOpen();
      cancelClose();
      if (current === t && pinned) { close(); return; }
      if (current !== t) open(t);
      pinned = true;
      return;
    }
    if (pinned && panel && !panel.contains(e.target)) close();
  }, true);

  // -- keyboard --------------------------------------------------------

  document.addEventListener("focusin", function (e) {
    if (panel && panel.contains(e.target)) { cancelClose(); return; }
    var t = closest(e.target, "[data-comment-peek]");
    if (!t) return;
    cancelClose();
    if (t !== current) open(t);  // no delay: focus is deliberate
  });

  document.addEventListener("focusout", function (e) {
    if (pinned || !current) return;
    var from = closest(e.target, "[data-comment-peek]") || (panel && panel.contains(e.target) ? panel : null);
    if (!from) return;
    var to = e.relatedTarget;
    if (to && ((current && current.contains(to)) || (panel && panel.contains(to)))) return;
    scheduleClose();
  });

  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape") {
      if (!current) return;
      var trigger = current;
      var fromPanel = panel && panel.contains(document.activeElement);
      close();
      if (fromPanel && trigger.isConnected) trigger.focus();
      e.stopPropagation();  // only swallowed while a panel was actually open
      return;
    }
    if (e.key !== "Enter" && e.key !== " ") return;
    var t = closest(e.target, "[data-comment-peek]");
    if (!t) return;
    e.preventDefault();
    if (current === t && pinned) { close(); t.focus(); return; }
    if (current !== t) open(t);
    pinned = true;
  }, true);

  // -- keep it where it belongs ----------------------------------------

  function reposition(e) {
    if (!current || !panel || panel.hidden) return;
    if (e && e.target && panel.contains && panel.contains(e.target)) return;  // scrolling IN the panel
    if (rafPending) return;
    rafPending = true;
    requestAnimationFrame(function () { rafPending = false; position(); });
  }
  window.addEventListener("scroll", reposition, true);
  window.addEventListener("resize", reposition);

  // A refresh re-renders the rows (and may have added a comment): the chips
  // this panel points at are about to be replaced, and what was cached is
  // possibly stale.
  document.addEventListener("bktodo:refresh-needed", function () {
    cache.clear();
    inflight.clear();
    close();
  });
})();
