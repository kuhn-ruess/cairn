/* Vim keys for the outliner.
 *
 * A BLOCK IS A LINE. That is the whole idea. Vim's line-wise commands act on
 * the block — ``dd`` takes it and everything under it, ``yy`` copies it, ``p``
 * puts it back, ``>>`` indents it — and the character-wise ones act inside the
 * textarea the open block already is. Nothing here parses or serializes: it
 * moves the caret, edits one block's text, and asks ``outliner.js`` for
 * anything structural, so there is still exactly one place that knows what a
 * note is.
 *
 * WHO CALLS WHOM
 * ``outliner.js`` builds a host — the operations this file is not allowed to
 * reimplement — and hands it every keystroke FIRST, before the "/" menu and
 * the "[[" completion, which must not see a key that is a command right now.
 * In insert mode this file only watches for Escape; Tab, Enter, the popups and
 * everything else are exactly as they were without it.
 *
 * THE COMMAND LINE
 * The bar in the corner is the mode indicator and the ``/`` prompt in one,
 * because in vim they are the same line. It is built here, so a page that does
 * not load this file has no trace of it.
 *
 * WHAT IS NOT HERE
 * Visual-line (V), marks, macros, named registers, ``.``. The unnamed register
 * is real, and it holds either text or whole block subtrees — ``p`` knows
 * which. Undo is a stack of model snapshots (u / Ctrl-r), separate from the
 * browser's own undo inside a block.
 */
(function () {
  const WORD = /[\wÀ-ɏ]/;          // letters, digits, "_", accented

  window.CairnVim = { attach };

  function attach(host) {
    let mode = "normal";            // normal | insert | visual
    let count = "";                 // digits typed before a command
    let op = "";                    // d | c | y, waiting for a motion
    let prefix = "";                // g z r f F > < — a key needing one more
    let anchor = null;              // where visual mode started
    let head = null;                // and where it has moved to since
    let reg = { text: "", trees: null };
    let undoStack = [], redoStack = [];
    let last = { term: "", back: false };
    let prompt = null;              // { term, back } while "/" is open
    let clicked = false;            // a click opened this block → insert

    const bar = buildBar();
    document.addEventListener("mousedown", () => {
      clicked = true;
      setTimeout(() => { clicked = false; }, 0);
    });

    // ---- the bar -----------------------------------------------------------

    function buildBar() {
      const el = document.createElement("div");
      el.className = "vim-bar";
      el.innerHTML = '<span class="vim-mode"></span><span class="vim-cmd"></span>';
      document.body.appendChild(el);
      return { el, mode: el.querySelector(".vim-mode"), cmd: el.querySelector(".vim-cmd") };
    }

    function paint() {
      bar.el.dataset.mode = mode;
      bar.mode.textContent = mode === "insert" ? "-- INSERT --"
                           : mode === "visual" ? "-- VISUAL --" : "NORMAL";
      bar.cmd.textContent = prompt ? (prompt.back ? "?" : "/") + prompt.term
                                   : count + op + prefix;
    }

    // ---- caret -------------------------------------------------------------

    const ta = () => host.textarea();
    const at = (t) => t.selectionStart;
    const n = () => Math.max(1, parseInt(count || "1", 10));

    /** Normal mode wants a BLOCK cursor, which a textarea can only draw as a
     *  one-character selection. Every motion goes through here, so the cursor
     *  never turns back into a thin line halfway through a command. */
    function caret(t, pos) {
      if (!t) return;
      const text = t.value;
      const p = Math.max(0, Math.min(pos, text.length));
      const ch = text.charAt(p);
      if (mode === "normal" && ch && ch !== "\n") t.setSelectionRange(p, p + 1);
      else t.setSelectionRange(p, p);
    }

    function dress(t) {
      if (t) t.classList.toggle("vim-normal", mode === "normal");
    }

    function toInsert(t, pos) {
      mode = "insert";
      dress(t);
      if (t && pos != null) t.setSelectionRange(pos, pos);
      paint();
    }

    function toNormal(t, { step = false } = {}) {
      const pos = t ? at(t) : 0;
      mode = "normal";
      anchor = head = null;
      dress(t);
      caret(t, step ? Math.max(0, pos - 1) : pos);
      paint();
    }

    // ---- text inside one block ---------------------------------------------

    const lineStart = (s, i) => s.lastIndexOf("\n", Math.max(0, i - 1)) + 1;
    const lineEnd = (s, i) => (s.indexOf("\n", i) < 0 ? s.length : s.indexOf("\n", i));
    function firstNonBlank(s, i) {
      let p = lineStart(s, i);
      while (p < s.length && /[ \t]/.test(s[p])) p += 1;
      return p;
    }
    const klass = (c) => (!c || /\s/.test(c) ? 0 : WORD.test(c) ? 1 : 2);

    function wordFwd(s, i) {
      let p = i, k = klass(s[p]);
      while (p < s.length && klass(s[p]) === k && k !== 0) p += 1;
      while (p < s.length && klass(s[p]) === 0) p += 1;
      return Math.min(p, s.length);
    }
    function wordEnd(s, i) {
      let p = i + 1;
      while (p < s.length && klass(s[p]) === 0) p += 1;
      const k = klass(s[p]);
      while (p + 1 < s.length && klass(s[p + 1]) === k && k !== 0) p += 1;
      return Math.min(p, Math.max(0, s.length - 1));
    }
    function wordBack(s, i) {
      let p = i - 1;
      while (p > 0 && klass(s[p]) === 0) p -= 1;
      const k = klass(s[p]);
      while (p > 0 && klass(s[p - 1]) === k && k !== 0) p -= 1;
      return Math.max(0, p);
    }

    // ---- writing -----------------------------------------------------------

    function write(node, t, text, pos) {
      t.value = text;
      host.setText(node, text);
      caret(t, pos);
    }

    function remember() {
      undoStack.push(host.snapshot());
      if (undoStack.length > 100) undoStack.shift();
      redoStack = [];
    }

    // ---- motions -----------------------------------------------------------

    /** ``{pos, inclusive}`` for a character motion, ``{blocks: [..]}`` for a
     *  line-wise one, or null when the key is not a motion at all. */
    function motion(key, node, t, from) {
      const s = t.value, p = from == null ? at(t) : from, c = n();
      switch (key) {
        case "h": case "ArrowLeft": return { pos: Math.max(lineStart(s, p), p - c) };
        case "l": case "ArrowRight": return { pos: Math.min(lineEnd(s, p), p + c) };
        case "0": return { pos: lineStart(s, p) };
        case "^": return { pos: firstNonBlank(s, p) };
        case "$": return { pos: lineEnd(s, p), inclusive: false };
        case "w": { let x = p; for (let i = 0; i < c; i++) x = wordFwd(s, x); return { pos: x }; }
        case "e": { let x = p; for (let i = 0; i < c; i++) x = wordEnd(s, x); return { pos: x, inclusive: true }; }
        case "b": { let x = p; for (let i = 0; i < c; i++) x = wordBack(s, x); return { pos: x }; }
        case "j": case "ArrowDown": return { blocks: run(node, c, 1) };
        case "k": case "ArrowUp": return { blocks: run(node, c, -1) };
        default: return null;
      }
    }

    /** ``count`` blocks from ``node`` in ``dir`` — what dj / 3dd operate on. */
    function run(node, c, dir) {
      const order = host.order();
      const i = order.indexOf(node);
      if (i < 0) return [node];
      const a = dir > 0 ? i : Math.max(0, i - c);
      const b = dir > 0 ? Math.min(order.length, i + c + 1) : i + 1;
      return order.slice(a, b);
    }

    /** j/k with no operator: down a line inside the block while there is one,
     *  else into the next block, keeping the column. */
    function vertical(dir) {
      let steps = n();
      while (steps > 0) {
        const t = ta();
        if (!t) return;
        const s = t.value, p = at(t);
        const ls = lineStart(s, p), le = lineEnd(s, p), col = p - ls;
        if (dir > 0 && le < s.length) {
          caret(t, Math.min(le + 1 + col, lineEnd(s, le + 1)));
        } else if (dir < 0 && ls > 0) {
          const pls = lineStart(s, ls - 1);
          caret(t, Math.min(pls + col, ls - 1));
        } else {
          const cur = host.editing();
          const target = dir > 0 ? host.next(cur) : host.prev(cur);
          if (!target) return;
          host.open(target, 0);
          const nt = ta();
          if (!nt) return;
          dress(nt);
          const s2 = nt.value;
          const base = dir > 0 ? 0 : lineStart(s2, s2.length);
          caret(nt, Math.min(base + col, lineEnd(s2, base)));
        }
        steps -= 1;
      }
    }

    function goTo(node, pos) {
      // Never leave the note without an open block: the keys would go to the
      // page, where "p" is quick-jump and "g r" is a different screen.
      node = node || host.order()[0];
      if (!node) return;
      host.open(node, pos || 0);
      const t = ta();
      dress(t);
      caret(t, pos || 0);
    }

    // ---- operators ---------------------------------------------------------

    function charOperator(kind, node, t, m) {
      const p = at(t);
      const from = Math.min(p, m.pos);
      const to = Math.max(p, m.pos) + (m.inclusive ? 1 : 0);
      if (from === to) return;
      reg = { text: t.value.slice(from, to), trees: null };
      if (kind === "y") { caret(t, from); return; }
      remember();
      write(node, t, t.value.slice(0, from) + t.value.slice(to), from);
      if (kind === "c") toInsert(t, from);
    }

    function lineOperator(kind, node, blocks) {
      const targets = blocks && blocks.length ? blocks : [node];
      if (kind === "y") {
        reg = { text: "", trees: host.copyTrees(targets) };
        host.status(`${targets.length} block(s) yanked`);
        return;
      }
      remember();
      if (kind === "c") {
        // cc empties the block and leaves you writing in it, children intact.
        const t = ta();
        reg = { text: node.content || "", trees: null };
        write(node, t, "", 0);
        toInsert(t, 0);
        return;
      }
      reg = { text: "", trees: host.copyTrees(targets) };
      const after = host.afterRemoving(targets);
      host.removeNodes(targets);
      goTo(after, 0);
    }

    function put(node, t, before) {
      remember();
      if (reg.trees && reg.trees.length) {
        goTo(host.putTrees(node, reg.trees, before), 0);
        return;
      }
      if (!reg.text) return;
      const p = at(t);
      const cut = before ? p : Math.min(t.value.length, p + 1);
      write(node, t, t.value.slice(0, cut) + reg.text + t.value.slice(cut),
            cut + reg.text.length - 1);
    }

    // ---- search ------------------------------------------------------------

    function find(term, back, node, from) {
      if (!term) return;
      last = { term, back: !!back };
      const cased = term !== term.toLowerCase();     // smartcase, as in vim
      const needle = cased ? term : term.toLowerCase();
      const hay = (s) => (cased ? s : s.toLowerCase());
      const order = host.order();
      const start = Math.max(0, order.indexOf(node));
      for (let step = 0; step <= order.length; step += 1) {
        const i = back ? ((start - step) % order.length + order.length) % order.length
                       : (start + step) % order.length;
        const s = hay(order[i].content || "");
        const hit = step === 0
          ? (back ? s.lastIndexOf(needle, Math.max(0, from - 1)) : s.indexOf(needle, from + 1))
          : (back ? s.lastIndexOf(needle) : s.indexOf(needle));
        if (hit >= 0) { goTo(order[i], hit); host.status((back ? "?" : "/") + term); return; }
      }
      host.status(`Pattern not found: ${term}`);
    }

    // ---- keys --------------------------------------------------------------

    function handleKey(e, node, t) {
      if (prompt) return promptKey(e, node, t);

      if (mode === "insert") {
        if (e.key === "Escape" || (e.ctrlKey && e.key === "[")) {
          e.preventDefault();
          toNormal(t, { step: true });
          host.save(node);
          return true;
        }
        return false;
      }

      // Normal / visual. The browser's own shortcuts still belong to the
      // browser; every other key on the keyboard is a command right now.
      if (e.metaKey || e.altKey) return false;
      if (e.ctrlKey && e.key !== "r" && e.key !== "[") return false;
      if (["Shift", "Control", "Alt", "Meta", "CapsLock"].indexOf(e.key) >= 0) return false;

      e.preventDefault();
      const k = e.key;

      if (prefix) return pending(k, node, t);

      if (/[1-9]/.test(k) || (k === "0" && count)) { count += k; paint(); return true; }

      if (k === "Escape" || (e.ctrlKey && k === "[")) {
        if (count || op || mode === "visual") { count = op = ""; toNormal(t); return true; }
        host.close();
        return true;
      }
      if (e.ctrlKey && k === "r") { restore(redoStack, undoStack); return true; }

      const m = motion(k, node, t, mode === "visual" ? head : null);
      if (m) {
        if (op) {
          if (m.blocks) lineOperator(op, node, m.blocks);
          else charOperator(op, node, t, m);
          op = ""; count = ""; paint();
        } else if (m.blocks) {
          vertical(k === "j" || k === "ArrowDown" ? 1 : -1);
          count = ""; paint();
        } else if (mode === "visual") {
          // The selection grows from the anchor to a head of its own: reading
          // it back off selectionStart would leave every motion after the
          // first measuring from the anchor and never moving.
          head = m.pos;
          t.setSelectionRange(Math.min(anchor, head), Math.max(anchor, head) + 1);
          count = ""; paint();
        } else {
          caret(t, m.pos); count = ""; paint();
        }
        return true;
      }

      switch (k) {
        case "i": toInsert(t, at(t)); break;
        case "a": toInsert(t, Math.min(t.value.length, at(t) + 1)); break;
        case "I": toInsert(t, firstNonBlank(t.value, at(t))); break;
        case "A": toInsert(t, lineEnd(t.value, at(t))); break;
        case "v":
          if (mode === "visual") { toNormal(t); break; }
          anchor = head = at(t); mode = "visual"; dress(t);
          t.setSelectionRange(anchor, Math.min(t.value.length, anchor + 1));
          paint();
          break;

        case "d": case "c": case "y":
          if (mode === "visual") { visualOperator(k, node, t); break; }
          if (op === k) { lineOperator(k, node, run(node, n() - 1, 1)); op = ""; count = ""; }
          else { op = k; }
          paint();
          break;
        case "D": charOperator("d", node, t, { pos: lineEnd(t.value, at(t)) }); break;
        case "C": charOperator("c", node, t, { pos: lineEnd(t.value, at(t)) }); break;
        case "S": lineOperator("c", node, [node]); break;

        case "x": charOperator("d", node, t, { pos: Math.min(lineEnd(t.value, at(t)), at(t) + n() - 1), inclusive: true }); break;
        case "X": charOperator("d", node, t, { pos: Math.max(lineStart(t.value, at(t)), at(t) - n()) }); break;
        case "s": charOperator("c", node, t, { pos: Math.min(lineEnd(t.value, at(t)), at(t) + n() - 1), inclusive: true }); break;
        case "r": prefix = "r"; paint(); return true;
        case "J": join(node, t); break;
        case "p": put(node, t, false); break;
        case "P": put(node, t, true); break;
        case "u": restore(undoStack, redoStack); break;

        case "o": fresh(node, false); break;
        case "O": fresh(node, true); break;
        case "g": case "z": case "f": case "F": case ">": case "<":
          prefix = k; paint(); return true;
        case "G": goTo(count ? host.order()[n() - 1] : host.order()[host.order().length - 1], 0); break;

        case "/": prompt = { term: "", back: false }; paint(); return true;
        case "?": prompt = { term: "", back: true }; paint(); return true;
        case "n": find(last.term, last.back, node, at(t)); break;
        case "N": find(last.term, !last.back, node, at(t)); break;

        default: break;      // swallowed: an unknown command must not type itself
      }
      count = "";
      paint();
      return true;
    }

    /** The second key of ``r? f? g? z? >? <?``. */
    function pending(k, node, t) {
      const was = prefix;
      prefix = "";
      if (was === "r" && k.length === 1) {
        remember();
        const p = at(t);
        write(node, t, t.value.slice(0, p) + k + t.value.slice(p + 1), p);
      } else if ((was === "f" || was === "F") && k.length === 1) {
        const s = t.value, p = at(t);
        const to = was === "F" ? s.lastIndexOf(k, p - 1) : s.indexOf(k, p + 1);
        if (to >= 0) {
          if (op) { charOperator(op, node, t, { pos: to, inclusive: was === "f" }); op = ""; }
          else caret(t, to);
        }
      } else if (was === "g" && k === "g") {
        goTo(count ? host.order()[n() - 1] : host.order()[0], 0);
      } else if (was === "z" && (k === "a" || k === "c" || k === "o")) {
        host.toggleFold(node);
      } else if (was === ">" && k === ">") {
        remember();
        for (let i = 0; i < n(); i += 1) host.indent(node);
        goTo(node, at(t));
      } else if (was === "<" && k === "<") {
        remember();
        for (let i = 0; i < n(); i += 1) host.outdent(node);
        goTo(node, at(t));
      }
      count = "";
      paint();
      return true;
    }

    function promptKey(e, node, t) {
      e.preventDefault();
      if (e.key === "Escape") { prompt = null; paint(); return true; }
      if (e.key === "Enter") {
        const { term, back } = prompt;
        prompt = null; paint();
        find(term, back, node, at(t));
        return true;
      }
      if (e.key === "Backspace") { prompt.term = prompt.term.slice(0, -1); paint(); return true; }
      if (e.key.length === 1) { prompt.term += e.key; paint(); }
      return true;
    }

    function visualOperator(kind, node, t) {
      const a = t.selectionStart, b = t.selectionEnd;
      reg = { text: t.value.slice(a, b), trees: null };
      if (kind !== "y") {
        remember();
        write(node, t, t.value.slice(0, a) + t.value.slice(b), a);
      }
      if (kind === "c") { toInsert(t, a); return; }
      toNormal(t);
      caret(ta(), a);
    }

    function fresh(node, above) {
      remember();
      const made = above ? host.insertAbove(node) : host.insertBelow(node);
      if (!made) return;
      host.open(made, 0);
      toInsert(ta(), 0);
    }

    /** J — pull what follows onto this line: the next line of this block, or
     *  the next block when there is none (and only when nothing is orphaned). */
    function join(node, t) {
      const s = t.value, p = at(t), le = lineEnd(s, p);
      if (le < s.length) {
        remember();
        write(node, t, s.slice(0, le) + " " + s.slice(le + 1).replace(/^[ \t]+/, ""), le);
        return;
      }
      const next = host.next(node);
      if (!next || (next.children || []).length) { host.status("Nothing to join"); return; }
      remember();
      const joined = s ? `${s} ${next.content || ""}` : (next.content || "");
      write(node, t, joined, s.length);
      host.removeNodes([next]);
      goTo(node, s.length);
    }

    function restore(from, to) {
      const snap = from.pop();
      if (!snap) { host.status(from === undoStack ? "Already at oldest change" : "Already at newest change"); return; }
      to.push(host.snapshot());
      host.restore(snap);
      dress(ta());
      paint();
    }

    paint();

    return {
      handleKey,
      /** A block's textarea just opened. The mode carries over from wherever
       *  the writer was — except after a click, which is a click to write. */
      opened(node, t) {
        if (clicked && mode === "normal") mode = "insert";
        dress(t);
        if (mode === "normal") caret(t, at(t));
        paint();
      },
      closed() { paint(); },
      mode: () => mode,
    };
  }
})();
