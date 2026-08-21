/* The two popups that open while a block is being edited: the "/" command menu
 * (slash-menu.js) and the "[[" note completion.
 *
 * Both were written against CodeMirror, and still are — ``ed`` below is a
 * handle of that shape over whichever block's textarea is currently open. It is
 * attached ONCE rather than per block: slash-menu.attach() appends a popup
 * element to the page, so attaching per block would leave one behind for every
 * block the writer touches.
 *
 * A file rather than a page script because two pages carry an outline — a note
 * and a notes-FOLDER, which is a container for filing and for access and also a
 * page with a body of its own.
 */
(function () {
  const outline = document.getElementById("note-outline");
  if (!outline) return;
  const noteId = outline.dataset.noteId;

  function escapeHtml(s) {
    return (s || "").replace(/[&<>"']/g, c =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }
  function escapeAttr(s) { return escapeHtml(s).replace(/"/g, "&quot;"); }

  // The body is edited by static/js/outliner.js, one block at a time. Both
  // popups below were written against CodeMirror, so ``ed`` is one handle that
  // always points at whichever block's textarea is open — attached ONCE,
  // rather than per block, or the slash menu would leave a popup element
  // behind for every block the writer touches.
  const ed = {
    get cm() { return window.CairnOutliner ? window.CairnOutliner.active() : null; },
    getValue() { const c = this.cm; return c ? c.getValue() : ""; },
    getCursor() { const c = this.cm; return c ? c.getCursor() : { line: 0, ch: 0 }; },
    setCursor(pos) { const c = this.cm; if (c) c.setCursor(pos); },
    getLine(n) { const c = this.cm; return c ? c.getLine(n) : ""; },
    getRange(a, b) { const c = this.cm; return c ? c.getRange(a, b) : ""; },
    replaceRange(t, a, b) { const c = this.cm; if (c) c.replaceRange(t, a, b); },
    replaceSelection(t) { const c = this.cm; if (c) c.replaceSelection(t); },
    focus() { const c = this.cm; if (c) c.focus(); },
    cursorCoords() {
      const c = this.cm;
      return c ? c.cursorCoords() : { left: 0, top: 0, bottom: 0 };
    },
    on() {},
  };
  const slash = window.CairnSlashMenu ? window.CairnSlashMenu.attach(ed) : null;

  // What outliner.js calls as blocks open, change and close.
  window.CairnNoteEditing = {
    opened() {},
    changed() { wiki.check(); if (slash) slash.check(); },
    closed() { wiki.hide(); if (slash) slash.hide(); },
    handleKey(e) {
      if (wiki.handleKey(e)) return true;
      return slash ? slash.handleKey(e) : false;
    },
  };

  // ---- [[wiki link]] completion ----------------------------------------
  const wiki = (function () {
    const box = document.getElementById("wiki-complete");
    // The sheet is a CSS container (see notes/_nav.html), which by spec makes
    // it the containing block for absolutely positioned children — and this
    // popup is placed in page coordinates. Hang it off <body>, where the
    // slash menu already lives, so the two agree and neither can drift.
    if (box) document.body.appendChild(box);
    let items = [], active = -1, timer = null, open = false, from = null;

    function hide() { box.style.display = "none"; open = false; active = -1; }
    function render() {
      box.innerHTML = items.map((it, i) =>
        `<li class="list-group-item list-group-item-action py-1 small ${i === active ? "active" : ""}"
             role="button" data-title="${escapeAttr(it.title)}">${escapeHtml(it.title)}</li>`).join("");
    }
    function place() {
      const pos = ed.cursorCoords();
      box.style.left = `${pos.left}px`;
      box.style.top = `${pos.bottom + 4}px`;
      box.style.display = "block";
      open = true;
    }
    function pick(title) {
      ed.replaceRange(`${title}]]`, from, ed.getCursor());
      hide();
      ed.focus();
    }
    box.addEventListener("mousedown", (e) => {
      // mousedown, not click — the editor's blur handler would close us first.
      const li = e.target.closest("[data-title]");
      if (li) { e.preventDefault(); pick(li.dataset.title); }
    });

    return {
      hide,
      check() {
        if (!ed.cm) return;
        const cur = ed.getCursor();
        const line = ed.getRange({ line: cur.line, ch: 0 }, cur);
        const m = line.match(/\[\[([^\[\]]{0,60})$/);
        if (!m) { hide(); return; }
        from = { line: cur.line, ch: cur.ch - m[1].length };
        clearTimeout(timer);
        timer = setTimeout(async () => {
          const data = await fetch(`/notes/titles?q=${encodeURIComponent(m[1])}`)
            .then(r => r.json()).catch(() => ({ results: [] }));
          items = (data.results || []).filter(r => r.id !== noteId);
          if (!items.length) { hide(); return; }
          active = 0;
          render();
          place();
        }, 150);
      },
      handleKey(e) {
        if (!open) return false;
        if (e.key === "ArrowDown") { active = (active + 1) % items.length; render(); }
        else if (e.key === "ArrowUp") { active = (active - 1 + items.length) % items.length; render(); }
        else if (e.key === "Enter" || e.key === "Tab") { pick(items[active].title); }
        else if (e.key === "Escape") { hide(); }
        else return false;
        e.preventDefault();
        e.stopPropagation();
        return true;
      },
    };
  })();
})();
