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
  // Where the whole body is posted. A note posts to its own edit route; the
  // scratchpad is not an Item at all and posts to its own. Everything else
  // about editing is identical, which is the point of naming it.
  const saveUrl = root.dataset.saveUrl || `/notes/${noteId}/edit`;
  const canEdit = root.dataset.canEdit === "1";
  const titleInput = document.getElementById("note-title");
  const status = document.getElementById("note-save-status");
  const conflictBox = document.getElementById("note-conflict");

  const KEYWORDS = ["TODO", "DOING", "DONE"];
  const KEYWORD_RE = /^(TODO|DOING|DONE|LATER|NOW|WAITING|CANCELED)\b ?/;
  const CHECKBOX_RE = /^\[([ xX])\] ?/;
  // A block whose whole content is an embed — it has something under it even
  // though that something lives in the content, not in a children box.
  const EMBED_RE = /^\{\{\s*embed\s*\(\([0-9a-fA-F][0-9a-fA-F-]{7,63}\)\)\s*\}\}$/i;

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
  /** Is there anything under this block to fold away? */
  function holdsSomething(node) {
    return (node.children || []).length > 0
        || EMBED_RE.test((node.content || "").trim());
  }

  function paint(node) {
    node.el.classList.toggle("ol-folded", collapsed(node));
    rowOf(node).querySelector(".ol-bullet")
      .classList.toggle("ol-parent", holdsSomething(node));
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
    // A pair is a mark carried in from the rendered block (see the click
    // handler): what was marked stays marked, so it can be typed over.
    if (Array.isArray(caret)) {
      ta.setSelectionRange(caret[0], caret[1]);
    } else {
      const at = caret === "end" || caret === undefined ? ta.value.length : caret;
      ta.setSelectionRange(at, at);
    }
    ta.addEventListener("input", () => {
      grow(ta);
      node.content = ta.value;
      scheduleSave();
      if (window.CairnNoteEditing) window.CairnNoteEditing.changed();
    });
    ta.addEventListener("blur", () => { if (editing === node) commit(node); });
    ta.addEventListener("keydown", (e) => onKey(e, node, ta));
    ta.addEventListener("paste", (e) => onPaste(e, node, ta));
    // Moving the caret can open or close the popups too ("[[" is now behind
    // me"), which is what CodeMirror's cursorActivity used to cover.
    //
    // But NOT after a key the popup itself consumed. Arrowing down their list
    // never moved the caret, and re-reading the token would put the highlight
    // back on the first row — so the list looked stuck at the top no matter
    // how often you pressed down.
    ta.addEventListener("keyup", () => {
      if (popupAte) { popupAte = false; return; }
      if (window.CairnNoteEditing) window.CairnNoteEditing.changed();
    });
    if (window.CairnNoteEditing) window.CairnNoteEditing.opened();
    if (vim) vim.opened(node, ta);
  }

  function grow(ta) {
    ta.style.height = "auto";
    ta.style.height = ta.scrollHeight + "px";
  }

  /** Close the textarea and put the rendered block back. ``alsoRender`` are
   *  further blocks that changed at the same time — a paste puts several down
   *  at once, and they must all come back rendered rather than as raw text. */
  function commit(node, alsoRender) {
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
    if (vim) vim.closed();
    save({ render: alsoRender ? [node].concat(alsoRender) : node });
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

  function insertBefore(node, fresh) {
    const list = siblings(node);
    fresh.parent = node.parent;
    list.splice(indexOf(node), 0, fresh);
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

  /** Give a freshly made block and everything under it an element, in place.
   *  Their text shows raw until the save renders it, which is a moment, not a
   *  state — but it is never blank. */
  function placeTree(node) {
    const tree = allNodes([node]);
    tree.forEach((n) => {
      if (!n.el) { makeBlockEl(n); contentOf(n).textContent = n.content || ""; }
    });
    tree.forEach(place);
  }

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
    const at = indexOf(node);
    if (at < 0) return;      // already gone; splice(-1) would eat a sibling
    siblings(node).splice(at, 1);
    if (node.el) node.el.remove();
  }

  function toggleFold(node) {
    if (!holdsSomething(node)) return;
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

  // Did an open popup take the last key? Its keyup must then be left alone.
  let popupAte = false;

  function onKey(e, node, ta) {
    popupAte = false;
    // Vim first: in normal mode a key is a command, and the "/" menu or the
    // "[[" completion opening on one would be the same key doing two things.
    if (vim && vim.handleKey(e, node, ta)) return;
    if (window.CairnNoteEditing && window.CairnNoteEditing.handleKey(e)) {
      popupAte = true;
      return;
    }

    const at = ta.selectionStart;
    const atStart = at === 0 && ta.selectionEnd === 0;
    const atEnd = at === ta.value.length && ta.selectionEnd === ta.value.length;
    const firstLine = ta.value.lastIndexOf("\n", Math.max(0, at - 1)) < 0;
    const lastLine = ta.value.indexOf("\n", at) < 0;

    if ((e.metaKey || e.ctrlKey) && e.key === "s") {
      e.preventDefault(); commit(node); return;
    }
    if ((e.metaKey || e.ctrlKey) && (e.key === "a" || e.key === "A")) {
      // First press takes the block, a second takes the page. Each block is
      // its own field, so without this "select all" could never reach past
      // the line the caret happens to be on — and the whole note could not be
      // copied, let alone cut.
      //
      // "Already holds the whole block" is the trigger, which is what the
      // FIRST press just did. (Not ``atStart``: that means a collapsed caret
      // at position zero, and after a select-all the selection is spread.)
      const whole = ta.value.length > 0
        && ta.selectionStart === 0 && ta.selectionEnd === ta.value.length;
      if (!whole) return;
      e.preventDefault();
      commit(node);
      ta.blur();
      selectAllBlocks();
      return;
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

  /** Mark the whole outline, so copy and cut can act on all of it. */
  function selectAllBlocks() {
    const sel = window.getSelection();
    if (!sel) return;
    sel.removeAllRanges();
    const range = document.createRange();
    range.selectNodeContents(root.querySelector(":scope > .ol-list") || root);
    sel.addRange(range);
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

    // Anywhere on the row edits its block: to the right of a short line, or on
    // the empty line of a block with nothing in it yet.
    const row = e.target.closest(".ol-row");
    if (!row) {
      // The space under the outline puts the caret in the last block, which is
      // where writing continues.
      if (canEdit && !editing
          && (e.target === root || e.target.classList.contains("ol-list"))) {
        const order = visible(model);
        if (order.length) focusBlock(order[order.length - 1], "end");
      }
      return;
    }
    const box = row.querySelector(":scope > .ol-content");
    if (!box || !canEdit) return;
    // Things that are meant to be clicked stay clickable.
    if (e.target.closest("a, img, input")) return;
    // Marking text ends in a click too. A mark ACROSS blocks is left exactly
    // as it is — that is how you mark a passage to turn into tasks, and
    // opening an editor would throw it away at the moment the reader reached
    // for it. A mark inside ONE block is the opposite: it is an edit about to
    // happen (delete the word, type over it), so that block opens and the
    // mark comes along into its textarea.
    const sel = window.getSelection();
    if (sel && !sel.isCollapsed && root.contains(sel.anchorNode)) {
      const marked = markedBox();
      const inside = marked && nodeFor(marked);
      if (!inside) return;
      openEditor(inside, sourceRange(inside, marked, sel.getRangeAt(0)) || "end");
      return;
    }
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
    // Only the zoom changes. Assigning to location.search would drop the
    // folder, tag, search and sort the middle column is standing on, and the
    // reader would land on the same note with "all notes" beside it.
    const args = new URLSearchParams(window.location.search);
    args.set("zoom", node.props.id);
    window.location.search = args.toString();
  }

  /** Put a pointer at this block on the clipboard, minting its id first.
   *
   *  Two shapes, because they read differently where they land: a REFERENCE is
   *  an inline link carrying the block's text, an EMBED shows the block itself,
   *  children and all. An embed only renders as one when it is a block's whole
   *  content — written into a sentence it degrades to the link.
   */
  async function copyPointer(node, asEmbed) {
    if (!node.props.id) {
      node.props.id = freshId();
      paint(node);
      if (await save() === false) { delete node.props.id; paint(node); return; }
    }
    const text = asEmbed ? `{{embed ((${node.props.id}))}}` : `((${node.props.id}))`;
    try {
      await navigator.clipboard.writeText(text);
      setStatus(asEmbed ? "Embed copied" : "Reference copied");
      setTimeout(() => { if (!dirty()) setStatus(""); }, 2000);
    } catch (err) {
      window.prompt("Copy this:", text);
    }
  }

  // ---- the bullet's menu ---------------------------------------------------

  let bulletMenu = null;

  function closeBulletMenu() {
    if (bulletMenu) { bulletMenu.remove(); bulletMenu = null; }
  }

  function openBulletMenu(node, x, y) {
    closeBulletMenu();
    const el = document.createElement("div");
    el.className = "dropdown-menu show ol-bullet-menu";
    el.style.cssText = `position:absolute; left:${x}px; top:${y}px; z-index:2100;`;
    el.innerHTML =
      '<button type="button" class="dropdown-item small" data-act="ref">'
      + '<i class="bi bi-link-45deg"></i> Copy block reference'
      + '<span class="d-block text-muted" style="font-size:.75em;">a link carrying this text</span>'
      + "</button>"
      + '<button type="button" class="dropdown-item small" data-act="embed">'
      + '<i class="bi bi-box-arrow-in-down-right"></i> Copy block embed'
      + '<span class="d-block text-muted" style="font-size:.75em;">shows the block itself</span>'
      + "</button>"
      + '<div class="dropdown-divider"></div>'
      + '<button type="button" class="dropdown-item small" data-act="zoom">'
      + '<i class="bi bi-arrows-angle-expand"></i> Zoom in</button>';
    // mousedown would blur the block being edited and commit it out from under
    // the click.
    el.addEventListener("mousedown", (e) => e.preventDefault());
    el.addEventListener("click", (e) => {
      const btn = e.target.closest("[data-act]");
      if (!btn) return;
      const act = btn.dataset.act;
      closeBulletMenu();
      if (act === "zoom") zoomTo(node);
      else copyPointer(node, act === "embed");
    });
    document.body.appendChild(el);
    bulletMenu = el;
  }

  root.addEventListener("contextmenu", (e) => {
    const bullet = e.target.closest(".ol-bullet");
    if (!bullet || e.target.closest("[data-embed]")) return;
    const n = nodeFor(bullet);
    if (!n) return;
    e.preventDefault();
    openBulletMenu(n, e.pageX, e.pageY);
  });

  document.addEventListener("mousedown", (e) => {
    if (bulletMenu && !e.target.closest(".ol-bullet-menu")) closeBulletMenu();
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") closeBulletMenu();
  });

  // ---- taking blocks out and putting them back ----------------------------
  //
  // A marked range spans whole blocks (see ``selection``), so what leaves on
  // the clipboard is the outline's own Markdown — indentation and all — and
  // what arrives can be laid back down as blocks rather than as one long line.

  /** The block a mark sits INSIDE — a word, a phrase — or null when it spans
   *  blocks (its common ancestor is then above any single ``.ol-content``,
   *  which is also where ``selectAllBlocks`` leaves it).
   *
   *  Such a mark is one block's own text and belongs to that block: to the
   *  browser's clipboard, and to the block's editor. A reader who marked one
   *  word asked for that word, not for the block (with its subtree, in
   *  Markdown) — and not to be told they cannot delete it. */
  function markedBox() {
    const sel = window.getSelection();
    if (!sel || sel.isCollapsed || !sel.rangeCount) return null;
    const at = sel.getRangeAt(0).commonAncestorContainer;
    const el = at.nodeType === 1 ? at : at.parentElement;
    const box = el && el.closest(".ol-content");
    return box && !box.closest("[data-embed]") ? box : null;
  }

  /** Where a mark on a block's RENDERED text sits in its source.
   *
   *  The two are not the same string — ``**bold**`` reads as "bold", a
   *  checkbox as nothing at all — so the mark is looked up by its own text,
   *  from where it sits in the rendered line. Markup only ever makes the
   *  source longer, so the source offset is at or past that. A mark that
   *  straddled markup is not found and has no range. */
  function sourceRange(node, box, range) {
    const text = range.toString();
    if (!text) return null;
    const before = document.createRange();
    before.selectNodeContents(box);
    before.setEnd(range.startContainer, range.startOffset);
    const src = node.content || "";
    let at = src.indexOf(text, before.toString().length);
    if (at < 0) at = src.indexOf(text);
    return at < 0 ? null : [at, at + text.length];
  }

  document.addEventListener("copy", (e) => {
    if (editing) return;             // inside a block: the browser's own job
    if (markedBox()) return;         // a word out of one block: likewise
    const picked = selection();
    if (!picked.marked) return;      // nothing of OURS is marked: let it be
    e.clipboardData.setData("text/plain", picked.text);
    e.preventDefault();
  });

  document.addEventListener("cut", (e) => {
    if (!canEdit || editing) return;
    if (markedBox()) return;
    const picked = selection();
    if (!picked.marked) return;
    e.clipboardData.setData("text/plain", picked.text);
    e.preventDefault();
    window.getSelection().removeAllRanges();
    remove(picked.blocks);
    setStatus("Cut");
  });

  /** Text put down in a block. One line goes in where the caret is, as it
   *  always has; more than one is an outline and is laid down as blocks. */
  async function onPaste(e, node, ta) {
    const text = (e.clipboardData || window.clipboardData).getData("text/plain");
    if (!text || !/\r|\n/.test(text.trim())) return;   // single line: as before
    e.preventDefault();
    const fd = new FormData();
    fd.append("csrf_token", csrf());
    fd.append("text", text);
    const data = await fetch("/notes/parse", {
      method: "POST", body: fd, credentials: "same-origin",
      headers: { "X-Requested-With": "XMLHttpRequest" },
    }).then((r) => r.json()).catch(() => null);
    const fresh = data && data.blocks && data.blocks.length ? link(data.blocks) : null;
    if (!fresh) return;

    node.content = ta.value;
    let after = node;
    if (!node.content.trim()) {
      // An empty block is a place to put things, not a thing: replace it.
      const first = fresh.shift();
      node.content = first.content;
      node.props = first.props;
      first.children.forEach((kid) => { kid.parent = node; node.children.push(kid); });
      ta.value = node.content;
    }
    placeTree(node);
    fresh.forEach((block) => {
      insertAfter(after, block);
      placeTree(block);
      after = block;
    });
    commit(node, allNodes(fresh));
    repaintAll();
    focusBlock(after, "end");
  }

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
    const targets = opts.render ? [].concat(opts.render) : [];
    const at = new Map(targets.map((n) => [n, docIndex(n)]));
    const render = targets.map((n) => at.get(n)).join(",");
    if (!canEdit || conflicted) return true;
    if (!dirty() && !render) { setStatus(""); return true; }
    setStatus("Saving…");
    const fd = new FormData();
    fd.append("csrf_token", csrf());
    // Only when the page actually HAS a title field. A folder page has a body
    // but no title input, and the save route reads "title" as an instruction —
    // an empty one would rename the folder to "Untitled note".
    if (titleInput) fd.append("title", titleInput.value);
    fd.append("note", body());
    fd.append("base_stamp", stamp);
    if (render) fd.append("render", render);
    busy += 1;
    let resp, data;
    try {
      resp = await fetch(saveUrl, {
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
    // The name may have followed the body (views.notes._derived_title), so
    // show what came back — unless the reader is standing in that field, where
    // overwriting what they are typing would be the rudest thing this could do.
    if (titleInput && typeof data.title === "string") {
      if (document.activeElement !== titleInput && titleInput.value !== data.title) {
        titleInput.value = data.title;
        // The navigator is server-rendered and would otherwise carry the old
        // name until the next page load.
        const row = document.querySelector(".nn-row.active .nn-name");
        if (row) row.textContent = data.title;
      }
      // What the SERVER has, not what the field shows. A field left out of
      // step (you emptied it and the name went back to following the body)
      // has to stay dirty, or nothing would ever come back to correct it.
      savedTitle = data.title;
    }
    savedNote = data.note;
    stamp = data.stamp;
    // Not into a block the writer is back inside already. Indent, outdent and
    // move all commit a block and immediately re-open it, and the render that
    // was in flight would replace the textarea they are typing in.
    targets.forEach((n) => {
      const html = data.rendered && data.rendered[String(at.get(n))];
      if (editing !== n && html !== undefined) applyRendered(n, html);
    });
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
    if (titleInput) fd.append("title", titleInput.value);
    fd.append("note", body());
    fd.append("base_stamp", stamp);
    navigator.sendBeacon(saveUrl, fd);
  });

  // Pasting an image or dropping a file uploads it, which saves the note on
  // the server (note-paste.js). Take the new stamp along, or the next autosave
  // reports a conflict against a version WE caused.
  document.addEventListener("cairn:item-saved", (e) => {
    if (e.detail && e.detail.stamp) stamp = e.detail.stamp;
  });

  const reload = document.getElementById("note-conflict-reload");
  if (reload) reload.addEventListener("click", () => window.location.reload());

  // Clicking anywhere off the outline closes the open block — except on the
  // things that ACT on it, which need it open (and marked) when they run.
  document.addEventListener("mousedown", (e) => {
    if (!editing) return;
    if (e.target.closest("#note-outline, #wiki-complete, .slash-menu, #note-title,"
                         + " [data-keeps-block-open]")) return;
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

  /** The blocks a text selection touches, and their Markdown.
   *
   *  The scratchpad converts what you marked into tasks or a note and takes it
   *  OUT of the pad. Marking spans rendered blocks, so the unit is the block:
   *  whatever the selection touched, whole, subtrees included. Nothing marked
   *  means everything — the pad's own rule. */
  function selection() {
    const sel = window.getSelection();
    const nodes = [];
    // A mark inside one block lives in that block's textarea (the click
    // handler puts it there), where the document has no selection to read.
    const ta = editing ? contentOf(editing).querySelector("textarea") : null;
    if (ta && ta.selectionStart !== ta.selectionEnd) {
      return { marked: true, blocks: [editing], text: serialize([editing]) };
    }
    if (sel && !sel.isCollapsed && sel.rangeCount) {
      const range = sel.getRangeAt(0);
      allNodes().forEach((n) => {
        if (n.el && !n.el.closest("[data-embed]") && range.intersectsNode(n.el)) {
          nodes.push(n);
        }
      });
    }
    // Only the topmost of any chain — a subtree comes along with its root, and
    // converting a parent AND its child would create the child twice.
    const inside = new Set();
    nodes.forEach((n) => allNodes(n.children).forEach((c) => inside.add(c)));
    const roots = nodes.filter((n) => !inside.has(n));
    const marked = roots.length > 0;
    const picked = marked ? roots : model.slice();
    return { marked, blocks: picked, text: serialize(picked) };
  }

  /** Drop these blocks (and their subtrees) and save. */
  function remove(nodes) {
    (nodes || []).forEach(removeNode);
    if (!model.length) model = link([blank()]);
    // Whatever was zoomed or shown may have been among them.
    //
    // Not filtered when the scope IS the model: ``listFor`` asks "is this node
    // in the scope?" to decide that a block belongs in the outline's top list,
    // and a filtered copy answers no for every block inserted afterwards — the
    // model would grow a block the page never shows.
    const live = new Set(allNodes());
    if (scope !== model) scope = scope.filter((n) => live.has(n));
    if (!scope.length) scope = model;
    if (!allNodes(scope).every((n) => n.el)) rebuild();
    repaintAll();
    return save();
  }

  // ---- vim ------------------------------------------------------------------
  //
  // The keys themselves live in static/js/outliner-vim.js; what this file owns
  // is the model, so vim gets a host of exactly the operations it must not
  // reimplement. Two clones with different rules: a SNAPSHOT keeps block ids
  // (undo puts the same blocks back), a COPY drops them (a put makes new
  // blocks, and a duplicated id would break every reference pointing at one).

  function cloneTree(node, keepId) {
    const props = Object.assign({}, node.props || {});
    if (!keepId) delete props.id;
    return {
      content: node.content || "",
      props,
      children: (node.children || []).map((c) => cloneTree(c, keepId)),
    };
  }

  function snapshot() {
    const ta = editing ? contentOf(editing).querySelector("textarea") : null;
    return {
      nodes: model.map((n) => cloneTree(n, true)),
      at: editing ? docIndex(editing) : -1,
      caret: ta ? ta.selectionStart : 0,
      // A zoom is remembered the way attach() reads it — by the block's id.
      // Not by identity ("is scope the model array?"): a delete rebuilds that
      // array, and every undo after one would have snapped the page into a
      // zoom nobody asked for.
      zoom: root.dataset.zoom || "",
    };
  }

  function restoreSnapshot(snap) {
    if (editing) { editing.el.classList.remove("ol-editing"); editing = null; }
    model = link(snap.nodes.map((n) => cloneTree(n, true)));
    const zoomed = snap.zoom
      ? allNodes().find((n) => n.props.id === snap.zoom) : null;
    scope = zoomed ? [zoomed] : model;
    rebuild();
    repaintAll();
    const target = snap.at >= 0 ? allNodes()[snap.at] : null;
    if (target) focusBlock(target, snap.caret);
    // Everything on screen was just drawn from text; ask for it rendered back.
    save({ render: allNodes(scope) });
  }

  const vim = (root.dataset.vim === "1" && window.CairnVim && canEdit)
    ? window.CairnVim.attach({
      editing: () => editing,
      textarea: () => (editing ? contentOf(editing).querySelector("textarea") : null),
      order: () => visible(scope),
      prev: previousVisible,
      next: nextVisible,
      open: (node, caret) => focusBlock(node, caret),
      close: () => { if (editing) commit(editing); },
      status: (text) => setStatus(text),
      save: (node) => save(node ? { render: node } : undefined),
      indent: (node) => { if (indent(node)) { repaintAll(); } },
      outdent: (node) => { if (outdent(node)) { repaintAll(); } },
      toggleFold,
      snapshot,
      restore: restoreSnapshot,

      setText(node, text) {
        node.content = text;
        const ta = contentOf(node).querySelector("textarea");
        if (ta) grow(ta);
        scheduleSave();
        if (window.CairnNoteEditing) window.CairnNoteEditing.changed();
      },

      insertBelow(node) {
        const made = blank();
        // Same rule Enter follows: a block with children open under it gets
        // its new neighbour INSIDE, or the new line jumps over what it belongs to.
        if ((node.children.length && !collapsed(node))
            || (scope.indexOf(node) >= 0 && scope !== model)) insertFirstChild(node, made);
        else insertAfter(node, made);
        scheduleSave();
        return made;
      },

      insertAbove(node) {
        const made = blank();
        insertBefore(node, made);
        scheduleSave();
        return made;
      },

      removeNodes: (nodes) => remove(nodes),

      /** Where the caret belongs once ``nodes`` are gone: the next block
       *  outside them, else the one before. Not simply "the next block" — the
       *  next one is usually the first CHILD, which is being taken away with
       *  its parent, and a caret opened in a detached element is a keyboard
       *  quietly talking to the page instead of the note. */
      afterRemoving(nodes) {
        const doomed = new Set();
        nodes.forEach((n) => allNodes([n]).forEach((d) => doomed.add(d)));
        const order = visible(scope);
        const last = order.indexOf(nodes[nodes.length - 1]);
        for (let i = last + 1; i < order.length; i += 1) {
          if (!doomed.has(order[i])) return order[i];
        }
        const first = order.indexOf(nodes[0]);
        for (let i = first - 1; i >= 0; i -= 1) {
          if (!doomed.has(order[i])) return order[i];
        }
        return null;
      },

      copyTrees: (nodes) => nodes.map((n) => cloneTree(n, false)),

      putTrees(node, trees, before) {
        let anchor = node, first = null;
        const landed = [];
        trees.forEach((tree, i) => {
          const made = link([cloneTree(tree, false)], node.parent)[0];
          if (before && i === 0) insertBefore(node, made);
          else insertAfter(anchor, made);
          placeTree(made);
          anchor = made;
          first = first || made;
          allNodes([made], landed);
        });
        repaintAll();
        save({ render: landed });
        return first;
      },
    })
    : null;

  window.CairnOutliner = {
    save,
    active: () => (editing ? adapter(contentOf(editing).querySelector("textarea")) : null),
    getStamp: () => stamp,
    setStamp: (s) => { if (s) stamp = s; },
    text: () => body(),
    /** Empty means "no block says anything" — a lone blank block still
     *  serializes to "-\n", which is text but is not content. */
    isEmpty: () => !allNodes().some((n) => (n.content || "").trim()),
    selection,
    remove,
    /** Write a block's text from outside — the pad's checklist button.
     *  Through the open textarea when this is the block being edited, or what
     *  that textarea still shows would overwrite it again when it closes. */
    setText(node, text) {
      node.content = text;
      const ta = editing === node ? contentOf(node).querySelector("textarea") : null;
      if (ta) { ta.value = text; grow(ta); }
    },
    /** Save, and have these blocks rendered back — for a caller that edited
     *  their content directly (the pad's checklist button). */
    rerender: (nodes) => save({ render: nodes }),
    reset() {
      model = link([blank()]);
      scope = model;       // rebuild() walks scope; the old nodes are gone
      rebuild();
      repaintAll();
      focusBlock(model[0], 0);
      return save();
    },
    focusFirst() { focusBlock(scope[0] || model[0], "end"); },
  };

  // An empty note is almost always about to be written in. Last, because
  // opening an editor reaches for ``vim`` — doing it while that const is
  // still in its dead zone throws, and the rest of this file never runs.
  if (canEdit && !savedNote.trim()) {
    if (!model.length) rebuild();
    focusBlock(scope[0] || model[0], 0);
  }
})();
