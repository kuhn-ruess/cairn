/* The scratchpad window — the pad, kept on screen wherever you are.
 *
 * The window is a frame (layout/_scratchpad_panel.html) holding
 * /scratchpad/frame, so the editor inside it is the ordinary one and this file
 * never touches the pad's content. What it owns is the WINDOW: open/closed,
 * where it sits, how big it is — remembered in localStorage, because every
 * navigation builds a new page and the window has to come back exactly as it
 * was or it is not one window, it is a new one each time.
 *
 * On the pad's own page there is no panel to drive (the page is the pad).
 * There, "Pin as window" only sets the flag, and the window appears the moment
 * you go anywhere else.
 */
(function () {
  const KEY = "cairn.scratchpad.window";
  const MIN_W = 260, MIN_H = 220;
  const wide = () => window.matchMedia("(min-width: 576px)").matches;

  function load() {
    try { return JSON.parse(localStorage.getItem(KEY) || "{}") || {}; }
    catch (e) { return {}; }
  }
  function save(patch) {
    const next = Object.assign(load(), patch);
    try { localStorage.setItem(KEY, JSON.stringify(next)); } catch (e) { /* blocked */ }
    return next;
  }

  const panel = document.getElementById("scratchpadPanel");
  const toggle = document.getElementById("scratchpadToggle");
  // The pad page's own button: no panel here, just the flag.
  const pinBtn = document.getElementById("scratchpadWindow");

  function setToggleState(open) {
    if (!toggle) return;
    toggle.classList.toggle("active", !!open);
    toggle.setAttribute("aria-pressed", open ? "true" : "false");
  }

  // The pad's own page carries no panel — the page IS the pad. Both buttons
  // only flip the flag there, and the window is there the moment you leave.
  if (!panel) {
    const paint = () => {
      const on = !!load().open;
      setToggleState(on);
      if (!pinBtn) return;
      pinBtn.classList.toggle("active", on);
      pinBtn.innerHTML = on
        ? '<i class="bi bi-pip-fill"></i> Window pinned'
        : '<i class="bi bi-pip"></i> Pin as window';
      pinBtn.title = on
        ? "The pad follows you as a small window — click to unpin"
        : "Keep the pad in a small window that follows you through Cairn";
    };
    [toggle, pinBtn].forEach((btn) => btn && btn.addEventListener("click", () => {
      save({ open: !load().open });
      paint();
    }));
    paint();
    return;
  }

  const frame = panel.querySelector(".sp-frame");
  const head = document.getElementById("scratchpadPanelHead");

  /** Put the window where it was left, kept inside the viewport. Narrow
   *  screens ignore all of it: there the CSS docks the pad to the bottom. */
  function place(state) {
    if (!wide()) {
      panel.style.cssText = "";
      return;
    }
    const w = Math.max(MIN_W, Math.min(state.w || 380, window.innerWidth - 16));
    const h = Math.max(MIN_H, Math.min(state.h || 440, window.innerHeight - 16));
    const left = state.left == null ? window.innerWidth - w - 24 : state.left;
    const top = state.top == null ? window.innerHeight - h - 24 : state.top;
    panel.style.width = w + "px";
    panel.style.height = h + "px";
    panel.style.left = Math.max(0, Math.min(left, window.innerWidth - w)) + "px";
    panel.style.top = Math.max(0, Math.min(top, window.innerHeight - h)) + "px";
  }

  function open() {
    // The frame is loaded on first open and then left alone — reloading it
    // would throw away whatever is half-typed in the pad.
    if (!frame.src) frame.src = panel.dataset.frameUrl;
    panel.hidden = false;
    place(load());
    setToggleState(true);
  }

  function close() {
    panel.hidden = true;
    setToggleState(false);
  }

  if (toggle) {
    toggle.addEventListener("click", () => {
      const next = !load().open;
      save({ open: next });
      next ? open() : close();
    });
  }
  panel.querySelector("[data-sp-close]").addEventListener("click", () => {
    save({ open: false });
    close();
  });

  // ---- drag by the title bar ----------------------------------------------
  head.addEventListener("pointerdown", (ev) => {
    if (ev.target.closest(".sp-btn") || !wide()) return;
    const rect = panel.getBoundingClientRect();
    const dx = ev.clientX - rect.left, dy = ev.clientY - rect.top;
    head.setPointerCapture(ev.pointerId);
    // The iframe would swallow the pointer the moment it crosses the frame.
    panel.classList.add("sp-dragging");

    const move = (e) => {
      const left = Math.max(0, Math.min(e.clientX - dx, window.innerWidth - rect.width));
      const top = Math.max(0, Math.min(e.clientY - dy, window.innerHeight - rect.height));
      panel.style.left = left + "px";
      panel.style.top = top + "px";
    };
    const up = () => {
      head.removeEventListener("pointermove", move);
      head.removeEventListener("pointerup", up);
      panel.classList.remove("sp-dragging");
      save({ left: parseInt(panel.style.left, 10), top: parseInt(panel.style.top, 10) });
    };
    head.addEventListener("pointermove", move);
    head.addEventListener("pointerup", up);
    ev.preventDefault();
  });

  // ---- resize: the browser's own grip (CSS ``resize``), we only remember it -
  if (window.ResizeObserver) {
    let timer = null;
    new ResizeObserver(() => {
      if (panel.hidden || !wide()) return;
      clearTimeout(timer);
      timer = setTimeout(() => save({ w: panel.offsetWidth, h: panel.offsetHeight }), 300);
    }).observe(panel);
  }

  window.addEventListener("resize", () => { if (!panel.hidden) place(load()); });

  if (load().open) open();
})();
