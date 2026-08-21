/* The note body as an outline — Logseq's editing model over Cairn's notes.
 *
 * WHO OWNS WHAT
 * The SERVER parses (services/outline.py) and hands the block tree down as
 * JSON next to the rendered outline; this file only ever SERIALIZES back.
 * Two parsers that could disagree about where a block starts is the one thing
 * this feature cannot afford, so there is only one, and it is the Python one.
 * ``serialize`` below is the exact mirror of ``outline.serialize`` — one tab
 * per level, a block's further lines two spaces past its bullet, properties
 * after the content.
 *
 * Each model node carries ``el`` — its ``.ol-block`` element. Structural edits
 * (indent, move, fold) move that element rather than re-rendering, so a block's
 * images and code keep their DOM and nothing flickers. Only a block whose TEXT
 * changed is re-rendered, and only that block: the server is asked for it by
 * document position via ``render=<idx>``.
 *
 * Saving is unchanged from the document editor it replaces — the whole body,
 * throttled, against ``base_stamp``, so the conflict check and the pagehide
 * beacon still do their jobs.
 */
(function () {
  const root = document.getElementById("note-outline");
  if (!root) return;

  const noteId = root.dataset.noteId;
  const canEdit = root.dataset.canEdit === "1";
  const titleInput = document.getElementById("note-title");
  const status = document.getElementById("note-save-status");
  const conflictBox = document.getElementById("note-conflict");

  const KEYWORDS = ["TODO", "DOING", "DONE"];
  const KEYWORD_RE = /^(TODO|DOING|DONE|LATER|NOW|WAITING|CANCELED)\b ?/;
  const CHECKBOX_RE = /^\[([ xX])\] ?/;

  let model = [];              // the WHOLE note — what gets serialized back
  let scope = [];              // the blocks actually on screen (zoom shows one)
  let stamp = root.dataset.stamp || "";
  let savedNote = "";
  let savedTitle = titleInput ? titleInput.value : "";
  let editing = null;          // the node whose textarea is open
  let saveTimer = null, queue = Promise.resolve(), conflicted = false;
  let busy = 0;                // saves of ours currently on the wire

  const csrf = () => (document.querySelector('input[name="csrf_token"]') || {}).value || "";

  // ---- serialize: the mirror of services/outline.serialize -----------------

  function serialize(nodes) {
    const out = [];
    (function emit(list, depth) {
      const pad = "\t".repeat(depth);
      list.forEach((b) => {
        const lines = (b.content || "").split("\n");
        out.push(lines[0] ? `${pad}- ${lines[0]}` : `${pad}-`);
        for (let i = 1; i < lines.length; i++) out.push(`${pad}  ${lines[i]}`);
        Object.keys(b.props || {}).forEach((k) => out.push(`${pad}  ${k}:: ${b.props[k]}`));
        emit(b.children || [], depth + 1);
      });
    })(nodes, 0);
    return out.length ? out.join("\n") + "\n" : "";
  }

  function body() { return serialize(model); }

  // ---- the tree ------------------------------------------------------------

  function link(nodes, parent) {
    nodes.forEach((n) => {
      n.parent = parent || null;
      n.props = n.props || {};
      n.children = n.children || [];
      link(n.children, n);
    });
    return nodes;
  }

  function siblings(node) { return node.parent ? node.parent.children : model; }
  function indexOf(node) { return siblings(node).indexOf(node); }
  function collapsed(node) { return String(node.props.collapsed || "").toLowerCase() === "true"; }

  /** Every node on screen in document order, skipping what is folded away. */
  function visible(nodes, out) {
    out = out || [];
    (nodes || scope).forEach((n) => {
      out.push(n);
      if (!collapsed(n)) visible(n.children, out);
    });
    return out;
  }

  /** Document position, which is how the server addresses a block. */
  function docIndex(target) { return allNodes().indexOf(target); }

  // ---- DOM -----------------------------------------------------------------

  const rowOf = (n) => n.el.querySelector(":scope > .ol-row");
  const contentOf = (n) => n.el.querySelector(":scope > .ol-row > .ol-content");

  function childrenBox(node) {
    let box = node.el.querySelector(":scope > .ol-children");
    if (!box) {
      box = document.createElement("div");
      box.className = "ol-children";
      box.appendChild(makeList());
      node.el.appendChild(box);
    }
    return box;
  }

  function makeList() {
    const list = document.createElement("div");
    list.className = "ol-list";
    return list;
  }

  function listFor(node) {
    // Where this node's element belongs: the outline's own list if it is one of
    // the blocks the page is showing, else its parent's children box. Zoomed in,
    // that top-level list holds the zoom target rather than the note's roots.
    if (scope.indexOf(node) >= 0) return root.querySelector(":scope > .ol-list");
    if (!node.parent || !node.parent.el) return null;
    return childrenBox(node.parent).querySelector(":scope > .ol-list");
  }

  function makeBlockEl(node) {
    const el = document.createElement("div");
    el.className = "ol-block";
    el.innerHTML =
      '<div class="ol-row">'
      + '<span class="ol-fold" role="button" tabindex="-1" aria-hidden="true"></span>'
      + '<span class="ol-bullet" role="button" tabindex="-1" title="Zoom in"></span>'
      + '<div class="ol-content md-body"></div>'
      + "</div>";
    node.el = el;
    return el;
  }

  /** Re-read a node's element state from the model (fold marks, parent ring). */
  function paint(node) {
    node.el.classList.toggle("ol-folded", collapsed(node));
    const hasKids = (node.children || []).length > 0;
    rowOf(node).querySelector(".ol-bullet").classList.toggle("ol-parent", hasKids);
    const kids = node.el.querySelector(":scope > .ol-children");
    if (kids) kids.classList.toggle("d-none", collapsed(node));
    if (node.props.id) node.el.dataset.uuid = node.props.id;
    else delete node.el.dataset.uuid;
  }

  /** Paint everything and renumber in ONE walk.
   *
   *  Numbering runs over the WHOLE note, not just what is on screen: data-idx
   *  is a block's position in the document the server parses, and zoomed in
   *  there are blocks above the visible ones that still count. */
  function repaintAll() {
    allNodes().forEach((n, i) => {
      if (!n.el) return;
      n.el.dataset.idx = i;
      contentOf(n).dataset.idx = i;
      paint(n);
    });
  }

  function allNodes(list, out) {
    out = out || [];
    (list || model).forEach((n) => { out.push(n); allNodes(n.children, out); });
    return out;
  }

  /** Put ``node``'s element where the model now says it belongs. */
  function place(node) {
    if (!node.el) return;
    const list = listFor(node);
    if (!list) return;
    const at = indexOf(node);
    const before = siblings(node)[at + 1];
    if (before && before.el && before.el.parentNode === list) list.insertBefore(node.el, before.el);
    else list.appendChild(node.el);
  }

  // ---- attaching the model to the server-rendered DOM ----------------------

  function attach() {
    const raw = document.getElementById("note-blocks");
    try { model = link(JSON.parse(raw.textContent || "[]")); }
    catch (e) { model = []; }
    savedNote = body();
    // Zoomed in, the page shows ONE block and its children while the model
    // still holds the whole note — the body written back has to be complete.
    // So only the blocks on screen get an element; the rest stay in the model
    // with none, and every DOM operation steps over them.
    const zoom = root.dataset.zoom || "";
    const target = zoom ? allNodes().find((n) => n.props.id === zoom) : null;
    scope = target ? [target] : model;
    const els = Array.from(root.querySelectorAll(".ol-block"))
      .filter((el) => !el.closest("[data-embed]"));
    const shown = allNodes(scope);
    // Same tree, same walk: the macro emits blocks depth-first in exactly the
    // order allNodes() produces them.
    shown.forEach((n, i) => { n.el = els[i]; });
    if (!shown.length || shown.some((n) => !n.el)) rebuild();
  }

  /** Last resort: draw the whole outline from the model (empty note, or the
   *  DOM and the model got out of step). Bodies render as plain text until
   *  their next save, which is the honest fallback — never a blank page. */
  function rebuild() {
    root.innerHTML = "";
    const list = makeList();
    root.appendChild(list);
    if (!model.length) model = link([{ content: "", props: {}, children: [] }]);
    if (!scope.length) scope = model;
    const shown = allNodes(scope);
    shown.forEach((n) => {
      if (!n.el) makeBlockEl(n);
      contentOf(n).textContent = n.content || "";
    });
    shown.forEach(place);
    shown.forEach(paint);
  }

  // ---- rendering one block after it changed --------------------------------

  /** The keyword chip and the checkbox — drawn BESIDE a block's text, not in
   *  it, so they have to be rebuilt whenever the client re-renders a block.
   *  Mirrors what the macro in notes/_outline.html emits. */
  function affixes(node) {
    let out = "";
    const kw = KEYWORD_RE.exec(node.content || "");
    if (kw) {
      out += `<button type="button" class="ol-kw ol-kw-${kw[1].toLowerCase()}" `
           + `title="Change state">${kw[1]}</button>`;
    }
    const box = CHECKBOX_RE.exec((node.content || "").replace(KEYWORD_RE, ""));
    if (box) {
      out += '<input type="checkbox" class="ol-box form-check-input me-1"'
           + (box[1].toLowerCase() === "x" ? " checked" : "") + ">";
    }
    return out;
  }

  function applyRendered(node, html) {
    contentOf(node).innerHTML = affixes(node) + (html || "");
  }

  // ---- editing -------------------------------------------------------------

  function openEditor(node, caret) {
    if (!canEdit || !node) return;
    if (editing === node) return;
    if (editing) commit(editing);
    editing = node;
    const box = contentOf(node);
    const ta = document.createElement("textarea");
    ta.className = "ol-edit";
    ta.name = "note";           // note-paste.js binds image paste / link drop here
    ta.rows = 1;
    ta.value = node.content || "";
    box.innerHTML = "";
    box.appendChild(ta);
    node.el.classList.add("ol-editing");
    grow(ta);
    ta.focus();
    const at = caret === "end" || caret === undefined ? ta.value.length : caret;
    ta.setSelectionRange(at, at);
    ta.addEventListener("input", () => {
      grow(ta);
      node.content = ta.value;
      scheduleSave();
      if (window.CairnNoteEditing) window.CairnNoteEditing.changed();
    });
    ta.addEventListener("blur", () => { if (editing === node) commit(node); });
    ta.addEventListener("keydown", (e) => onKey(e, node, ta));
    // Moving the caret can open or close the popups too ("[[" is now behind
    // me"), which is what CodeMirror's cursorActivity used to cover.
    ta.addEventListener("keyup", () => {
      if (window.CairnNoteEditing) window.CairnNoteEditing.changed();
    });
    if (window.CairnNoteEditing) window.CairnNoteEditing.opened();
  }

  function grow(ta) {
    ta.style.height = "auto";
    ta.style.height = ta.scrollHeight + "px";
  }

  /** Close the textarea and put the rendered block back. */
  function commit(node) {
    const box = contentOf(node);
    const ta = box.querySelector("textarea");
    if (!ta) return;
    node.content = ta.value;
    if (editing === node) editing = null;
    node.el.classList.remove("ol-editing");
    // Show the raw text until the server hands back the rendered version, so
    // the block is never blank in between.
    box.textContent = node.content;
    if (window.CairnNoteEditing) window.CairnNoteEditing.closed();
    save({ render: node });
  }

  function focusBlock(node, caret) {
    if (!node) return;
    openEditor(node, caret);
  }

  // ---- structural operations ----------------------------------------------

  function insertAfter(node, fresh) {
    const list = siblings(node);
    fresh.parent = node.parent;
    list.splice(indexOf(node) + 1, 0, fresh);
    makeBlockEl(fresh);
    place(fresh);
    return fresh;
  }

  function insertFirstChild(node, fresh) {
    fresh.parent = node;
    node.children.unshift(fresh);
    makeBlockEl(fresh);
    place(fresh);
    return fresh;
  }

  function blank() { return { content: "", props: {}, children: [] }; }

  function indent(node) {
    const list = siblings(node);
    const at = indexOf(node);
    if (at <= 0) return false;
    const prev = list[at - 1];
    list.splice(at, 1);
    node.parent = prev;
    prev.children.push(node);
    delete prev.props.collapsed;      // never hide what was just put in there
    place(node);
    return true;
  }

  function outdent(node) {
    const parent = node.parent;
    // Not past what the page is showing: zoomed in, the target is the top of
    // the world, and moving a block above it would file it somewhere the
    // writer cannot see.
    if (!parent || scope.indexOf(node) >= 0) return false;
    const list = siblings(node);
    const at = indexOf(node);
    // Everything after it on this level follows it down — the standard
    // outliner move, and the only one that doesn't silently reorder the note.
    const trailing = list.splice(at + 1);
    list.splice(at, 1);
    trailing.forEach((t) => { t.parent = node; node.children.push(t); });
    const up = parent.parent ? parent.parent.children : model;
    node.parent = parent.parent;
    up.splice(up.indexOf(parent) + 1, 0, node);
    place(node);
    trailing.forEach(place);
    return true;
  }

  function moveBy(node, delta) {
    const list = siblings(node);
    const at = indexOf(node);
    const to = at + delta;
    if (to < 0 || to >= list.length) return false;
    list.splice(at, 1);
    list.splice(to, 0, node);
    place(node);
    list.forEach(place);
    return true;
  }

  function removeNode(node) {
    const list = siblings(node);
    list.splice(indexOf(node), 1);
    node.el.remove();
  }

  function toggleFold(node) {
    if (!node.children.length) return;
    if (collapsed(node)) delete node.props.collapsed;
    else node.props.collapsed = "true";
    paint(node);
    scheduleSave();
  }

  function cycleKeyword(node) {
    const m = KEYWORD_RE.exec(node.content || "");
    const rest = node.content.replace(KEYWORD_RE, "");
    const at = m ? KEYWORDS.indexOf(m[1]) : -1;
    // Off -> TODO -> DOING -> DONE -> off. A keyword this file doesn't know
    // (LATER, WAITING — Logseq's other ones) drops out on the first press
    // rather than being cycled into a vocabulary it isn't part of.
    const next = at < 0 ? KEYWORDS[0] : (at + 1 < KEYWORDS.length ? KEYWORDS[at + 1] : "");
    node.content = next ? `${next} ${rest}` : rest;
    return node.content;
  }

  // ---- keys ----------------------------------------------------------------

  function onKey(e, node, ta) {
    if (window.CairnNoteEditing && window.CairnNoteEditing.handleKey(e)) return;

    const at = ta.selectionStart;
    const atStart = at === 0 && ta.selectionEnd === 0;
    const atEnd = at === ta.value.length && ta.selectionEnd === ta.value.length;
    const firstLine = ta.value.lastIndexOf("\n", Math.max(0, at - 1)) < 0;
    const lastLine = ta.value.indexOf("\n", at) < 0;

    if ((e.metaKey || e.ctrlKey) && e.key === "s") {
      e.preventDefault(); commit(node); return;
    }
    if ((e.metaKey || e.ctrlKey) && e.key === "Enter") {
      e.preventDefault();
      ta.value = cycleKeyword(node);
      grow(ta); scheduleSave();
      return;
    }
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      // Split at the caret, the way every outliner does: what is left of it
      // stays, what is right of it starts the new block.
      const before = ta.value.slice(0, at), after = ta.value.slice(ta.selectionEnd);
      node.content = before;
      ta.value = before;
      const fresh = blank();
      fresh.content = after;
      // A block with children open beneath it gets its new sibling INSIDE,
      // or the new text would jump over everything it belongs to.
      // A block with children open beneath it gets its new sibling INSIDE, or
      // the new text would jump over everything it belongs to. Same for the
      // block a zoom is anchored on: its siblings are off screen.
      if ((node.children.length && !collapsed(node))
          || (scope.indexOf(node) >= 0 && scope !== model)) {
        insertFirstChild(node, fresh);
      } else {
        insertAfter(node, fresh);
      }
      commit(node);
      repaintAll();
      focusBlock(fresh, 0);
      return;
    }
    if (e.key === "Tab") {
      e.preventDefault();
      const moved = e.shiftKey ? outdent(node) : indent(node);
      if (moved) { node.content = ta.value; commit(node); repaintAll(); focusBlock(node, at); }
      return;
    }
    if (e.key === "Backspace" && atStart) {
      const prev = previousVisible(node);
      if (!prev || node.children.length) return;   // never orphan a subtree
      e.preventDefault();
      const join = (prev.content || "").length;
      prev.content = (prev.content || "") + ta.value;
      removeNode(node);
      if (editing === node) editing = null;
      repaintAll();
      focusBlock(prev, join);
      scheduleSave();
      return;
    }
    if (e.key === "ArrowUp" && firstLine && !e.altKey) {
      const prev = previousVisible(node);
      if (prev) { e.preventDefault(); commit(node); focusBlock(prev, "end"); }
      return;
    }
    if (e.key === "ArrowDown" && lastLine && !e.altKey) {
      const next = nextVisible(node);
      if (next) { e.preventDefault(); commit(node); focusBlock(next, 0); }
      return;
    }
    if (e.altKey && e.shiftKey && (e.key === "ArrowUp" || e.key === "ArrowDown")) {
      e.preventDefault();
      node.content = ta.value;
      if (moveBy(node, e.key === "ArrowUp" ? -1 : 1)) {
        commit(node); repaintAll(); focusBlock(node, at);
      }
      return;
    }
    if (e.key === "Escape") {
      e.preventDefault();
      commit(node);
      ta.blur();
    }
  }

  function previousVisible(node) {
    const order = visible(model);
    return order[order.indexOf(node) - 1] || null;
  }
  function nextVisible(node) {
    const order = visible(model);
    return order[order.indexOf(node) + 1] || null;
  }

  // ---- clicks --------------------------------------------------------------

  function nodeFor(el) {
    const block = el.closest(".ol-block");
    if (!block || block.closest("[data-embed]")) return null;
    return allNodes().find((n) => n.el === block) || null;
  }

  root.addEventListener("click", (e) => {
    if (e.target.closest("[data-embed]")) return;      // another note's blocks

    const fold = e.target.closest(".ol-fold");
    if (fold) { const n = nodeFor(fold); if (n) toggleFold(n); return; }

    const bullet = e.target.closest(".ol-bullet");
    if (bullet) {
      const n = nodeFor(bullet);
      if (n) zoomTo(n);
      return;
    }

    const kw = e.target.closest(".ol-kw");
    if (kw && canEdit) {
      const n = nodeFor(kw);
      // applyRendered draws the chip from the model, so the re-render that
      // comes back from the save is all this needs.
      if (n) { cycleKeyword(n); save({ render: n }); }
      return;
    }

    const box = e.target.closest(".ol-content");
    if (!box || !canEdit) return;
    // Things that are meant to be clicked stay clickable.
    if (e.target.closest("a, img, input")) return;
    const sel = window.getSelection();
    if (sel && !sel.isCollapsed && box.contains(sel.anchorNode)) return;
    const n = nodeFor(box);
    if (n) openEditor(n, "end");
  });

  // Checkboxes are flipped in the model and saved like any other edit — the
  // server-side ordinal toggle can't be used here, because it numbers the
  // boxes within ONE rendered body and an outline renders block by block.
  root.addEventListener("change", (e) => {
    if (!canEdit || e.target.closest("[data-embed]")) return;

    // The block's own box ("- [ ] …"), drawn beside the text by affixes().
    const own = e.target.closest(".ol-box");
    if (own) {
      const node = nodeFor(own);
      if (!node) return;
      const kw = (KEYWORD_RE.exec(node.content) || [""])[0];
      node.content = kw + node.content.slice(kw.length)
        .replace(CHECKBOX_RE, own.checked ? "[x] " : "[ ] ");
      scheduleSave();
      return;
    }

    // A Markdown checklist written INSIDE one block's text. The renderer
    // numbers those per block, and so does this.
    const box = e.target.closest("input[data-md-task]");
    if (!box) return;
    const node = nodeFor(box);
    if (!node) return;
    const kw = (KEYWORD_RE.exec(node.content) || [""])[0];
    const rest = node.content.slice(kw.length);
    // The block's own leading box is drawn beside the text, so it is not part
    // of the body those ordinals were counted over — step over it.
    const want = parseInt(box.dataset.mdTask, 10) + (CHECKBOX_RE.test(rest) ? 1 : 0);
    let seen = 0;
    node.content = kw + rest.replace(/\[([ xX])\]/g, (m) =>
      seen++ === want ? (box.checked ? "[x]" : "[ ]") : m);
    scheduleSave();
  });

  function freshId() {
    return crypto.randomUUID ? crypto.randomUUID()
      : String(Date.now()) + Math.random().toString(16).slice(2);
  }

  async function zoomTo(node) {
    // Zooming addresses a block by id, so a block nobody has pointed at yet
    // gets one here — and only if it lands, or the URL would name a block the
    // note does not contain and the page would quietly show everything.
    if (!node.props.id) {
      node.props.id = freshId();
      paint(node);
      if (await save() === false) { delete node.props.id; paint(node); return; }
    }
    window.location.search = `?zoom=${node.props.id}`;
  }

  /** "Copy block ref" — mints the id, saves, and puts ((id)) on the clipboard. */
  async function copyRef(node) {
    if (!node.props.id) {
      node.props.id = freshId();
      paint(node);
      await save();
    }
    try { await navigator.clipboard.writeText(`((${node.props.id}))`); }
    catch (err) { window.prompt("Block reference:", `((${node.props.id}))`); }
  }

  root.addEventListener("contextmenu", (e) => {
    const bullet = e.target.closest(".ol-bullet");
    if (!bullet || e.target.closest("[data-embed]")) return;
    const n = nodeFor(bullet);
    if (!n) return;
    e.preventDefault();
    copyRef(n);
  });

  // ---- saving --------------------------------------------------------------

  function setStatus(text, cls) {
    if (!status) return;
    status.className = "ms-auto " + (cls || "text-muted");
    status.textContent = text;
  }

  function dirty() {
    return body() !== savedNote || (titleInput && titleInput.value !== savedTitle);
  }

  function scheduleSave() {
    clearTimeout(saveTimer);
    setStatus("Unsaved changes", "text-warning");
    saveTimer = setTimeout(() => save(), 1200);
  }

  /** Saves run one at a time, in order.
   *
   *  Awaiting the previous FETCH is not enough: the stamp it returns is only
   *  installed a couple of async hops later, so a second save could still read
   *  the old one and be refused as a conflict it invented itself. Chaining the
   *  whole operation is what makes that impossible. */
  function save(opts) {
    clearTimeout(saveTimer);
    queue = queue.catch(() => {}).then(() => doSave(opts || {}));
    return queue;
  }

  async function doSave(opts, retried) {
    const render = opts.render ? String(docIndex(opts.render)) : "";
    if (!canEdit || conflicted) return true;
    if (!dirty() && !render) { setStatus(""); return true; }
    setStatus("Saving…");
    const fd = new FormData();
    fd.append("csrf_token", csrf());
    fd.append("title", titleInput ? titleInput.value : "");
    fd.append("note", body());
    fd.append("base_stamp", stamp);
    if (render) fd.append("render", render);
    busy += 1;
    let resp, data;
    try {
      resp = await fetch(`/notes/${noteId}/edit`, {
        method: "POST", body: fd,
        headers: { "X-Requested-With": "XMLHttpRequest", "X-Partial": "1" },
      });
      data = await resp.json().catch(() => null);
    } finally {
      busy -= 1;
    }
    if (resp.status === 409) {
      // A version stamp can move without the TEXT moving. The usual cause is
      // us: a save that was still in flight when the page navigated lands
      // after the next page has already read its version, so that page holds a
      // stamp one step behind for a change it made itself. If what the server
      // has is exactly what we last saw, nothing diverged — take the new stamp
      // and go again, once.
      if (!retried && data && data.note === savedNote && data.title === savedTitle) {
        stamp = data.stamp;
        return doSave(opts, true);
      }
      conflicted = true;
      if (conflictBox) conflictBox.classList.remove("d-none");
      setStatus("Not saved — conflict", "text-danger");
      return false;
    }
    if (!data || !data.ok) {
      setStatus("Save failed — your text is still here", "text-danger");
      return false;
    }
    savedTitle = data.title;
    savedNote = data.note;
    stamp = data.stamp;
    // Not if the writer is back inside this block already. Indent, outdent and
    // move all commit the block and immediately re-open it, and the render
    // that was in flight would replace the textarea they are typing in.
    if (opts.render && editing !== opts.render
        && data.rendered && data.rendered[render] !== undefined) {
      applyRendered(opts.render, data.rendered[render]);
    }
    const words = document.getElementById("note-words");
    if (words) words.textContent = data.words;
    const updated = document.getElementById("note-updated");
    if (updated) updated.textContent = "just now";
    if (data.title) document.title = data.title + " · Notes · Cairn";
    setStatus("Saved");
    setTimeout(() => { if (!dirty()) setStatus(""); }, 2000);
    return true;
  }

  if (titleInput && !titleInput.readOnly) {
    titleInput.addEventListener("input", scheduleSave);
    titleInput.addEventListener("blur", () => save());
  }

  // Last-chance save when the tab goes away. sendBeacon survives unload where
  // fetch does not — without it, closing the tab mid-sentence loses the tail.
  window.addEventListener("pagehide", () => {
    if (!canEdit || conflicted) return;
    // Not while a save of ours is still on the wire. The beacon carries the
    // stamp we still hold, which that request is about to move — the server
    // would refuse it as a conflict, and the page we are leaving for would
    // start out one version behind. What is in flight already carries every
    // committed block; the only gap is the fraction of a second since it left,
    // and closing that by beaconing unconditionally would mean overwriting a
    // share partner who wrote in between. Losing a keystroke beats that.
    if (busy) return;
    if (editing) {
      const ta = contentOf(editing).querySelector("textarea");
      if (ta) editing.content = ta.value;
    }
    if (!dirty()) return;
    const fd = new FormData();
    fd.append("csrf_token", csrf());
    fd.append("title", titleInput ? titleInput.value : "");
    fd.append("note", body());
    fd.append("base_stamp", stamp);
    navigator.sendBeacon(`/notes/${noteId}/edit`, fd);
  });

  // Pasting an image or dropping a file uploads it, which saves the note on
  // the server (note-paste.js). Take the new stamp along, or the next autosave
  // reports a conflict against a version WE caused.
  document.addEventListener("cairn:item-saved", (e) => {
    if (e.detail && e.detail.stamp) stamp = e.detail.stamp;
  });

  const reload = document.getElementById("note-conflict-reload");
  if (reload) reload.addEventListener("click", () => window.location.reload());

  // Clicking anywhere off the outline closes the open block.
  document.addEventListener("mousedown", (e) => {
    if (!editing) return;
    if (e.target.closest("#note-outline, #wiki-complete, .slash-menu, #note-title")) return;
    commit(editing);
  });

  // ---- a CodeMirror-shaped handle on the open textarea --------------------
  // slash-menu.js and the [[ ]] completion were written against CodeMirror.
  // They use seven of its methods; this is those seven over a textarea, so
  // both menus keep working without knowing the editor changed underneath.

  function adapter(ta) {
    const posOf = (off) => {
      const before = ta.value.slice(0, off).split("\n");
      return { line: before.length - 1, ch: before[before.length - 1].length };
    };
    const offOf = (pos) => {
      const lines = ta.value.split("\n");
      let off = 0;
      for (let i = 0; i < pos.line && i < lines.length; i++) off += lines[i].length + 1;
      return off + pos.ch;
    };
    return {
      el: ta,
      getValue: () => ta.value,
      getCursor: () => posOf(ta.selectionStart),
      setCursor: (pos) => { const o = offOf(pos); ta.setSelectionRange(o, o); },
      getLine: (n) => ta.value.split("\n")[n] || "",
      getRange: (from, to) => ta.value.slice(offOf(from), offOf(to)),
      replaceRange(text, from, to) {
        const a = offOf(from), b = to ? offOf(to) : a;
        ta.value = ta.value.slice(0, a) + text + ta.value.slice(b);
        const at = a + text.length;
        ta.setSelectionRange(at, at);
        ta.dispatchEvent(new Event("input", { bubbles: true }));
      },
      replaceSelection(text) {
        const a = ta.selectionStart, b = ta.selectionEnd;
        ta.value = ta.value.slice(0, a) + text + ta.value.slice(b);
        const at = a + text.length;
        ta.setSelectionRange(at, at);
        ta.dispatchEvent(new Event("input", { bubbles: true }));
      },
      focus: () => ta.focus(),
      cursorCoords: () => caretCoords(ta),
      on: () => {},          // the menus drive themselves off our key handler
    };
  }

  /** Where the caret is on the page. A textarea won't say, so a mirror div
   *  with the same text and the same type styling is measured instead. */
  function caretCoords(ta) {
    const mirror = document.createElement("div");
    const cs = window.getComputedStyle(ta);
    ["fontFamily", "fontSize", "fontWeight", "lineHeight", "letterSpacing",
     "paddingTop", "paddingRight", "paddingBottom", "paddingLeft",
     "borderWidth", "boxSizing", "whiteSpace", "wordWrap"].forEach((p) => {
      mirror.style[p] = cs[p];
    });
    mirror.style.position = "absolute";
    mirror.style.visibility = "hidden";
    mirror.style.whiteSpace = "pre-wrap";
    mirror.style.wordWrap = "break-word";
    mirror.style.width = cs.width;
    mirror.textContent = ta.value.slice(0, ta.selectionStart);
    const marker = document.createElement("span");
    marker.textContent = "​";
    mirror.appendChild(marker);
    document.body.appendChild(mirror);
    const box = ta.getBoundingClientRect();
    const left = box.left + window.scrollX + marker.offsetLeft;
    const top = box.top + window.scrollY + marker.offsetTop - ta.scrollTop;
    const line = parseFloat(cs.lineHeight) || 20;
    mirror.remove();
    return { left, top, bottom: top + line };
  }

  // ---- boot ----------------------------------------------------------------

  attach();
  repaintAll();

  // An empty note is almost always about to be written in.
  if (canEdit && !savedNote.trim()) {
    if (!model.length) rebuild();
    focusBlock(scope[0] || model[0], 0);
  }

  window.CairnOutliner = {
    save,
    active: () => (editing ? adapter(contentOf(editing).querySelector("textarea")) : null),
    getStamp: () => stamp,
    setStamp: (s) => { if (s) stamp = s; },
  };
})();
