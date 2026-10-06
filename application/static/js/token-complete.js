/* The pick list for the `@person` and `+tag` tokens, for any text field.
 *
 * `CairnTokenComplete.attach(field, options)` gives a field the dropdown the
 * Quick Add dialog has: typing `@` lists teammates (the same list as an
 * @-mention in a comment, /tasks/assignees), typing `+` lists tags
 * (/tasks/tag-names, which also writes a name with spaces quoted). Picking an
 * entry replaces the partial token with the whole one. Quick Add
 * (`quick-add.js`) and the project sketch (`projects/sketch.html`) both use
 * it, so there is one list and one set of keys.
 *
 * **Nothing in the list is highlighted until the user arrows into it.** The
 * box completes a token inside a line the user is writing, and the two keys
 * that would take the highlighted entry, Enter and Tab, already mean
 * something in the field (create the task, leave the field, a new list line,
 * indent). A pre-selected first row makes Enter hand over a tag or a person
 * that was never picked, and silently, because `+cair` is perfectly good text
 * on its own. So the index stays -1 until ↓/↑ moves it, and while it is -1
 * the dropdown handles no key the field wanted for itself. A command bar is
 * the other case and keeps its pre-selection: quickjump (`quickjump.js`), the
 * `/` menu and the `[[ ]]` completion open BECAUSE a list was asked for.
 *
 * A key the list takes is `preventDefault`ed and stopped, so the field's own
 * handlers registered after `attach` should check `ev.defaultPrevented`.
 *
 * Options:
 *   sources   extra token kinds beside `user` and `tag`, same shape
 *             ({re, url, items} or {re, local}); an item with `pick` runs it
 *   place     function(box) that puts the box into the page (default: right
 *             after the field)
 *   position  function(box, field, token) called before the box is shown
 *   onApply   function() after an entry was taken
 */
(function () {
  function escape(s) {
    return String(s).replace(/[&<>]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));
  }

  const BASE_SOURCES = {
    user: {
      // Either half of a quoted display name, or a bare word.
      re: /(?:^|\s)(@"[^"\n]*|@[\w.\-äöüÄÖÜß]*)$/,
      url: q => `/tasks/assignees?q=${encodeURIComponent(q)}`,
      items: data => (data.users || []).map(u => ({
        token: "@" + u.username,
        html: '<i class="bi bi-person"></i> '
          + (u.display_name && u.display_name !== u.username
             ? `${escape(u.username)} <span class="text-muted">${escape(u.display_name)}</span>`
             : escape(u.username))
          + (u.is_me ? ' <span class="text-muted">(me)</span>' : ""),
      })),
    },
    tag: {
      // Either half of a quoted name, or a bare word, colons included, so the
      // list keeps answering while `+AI:local` is being typed.
      re: /(?:^|\s)(\+"[^"\n]*|\+[\w\-äöüÄÖÜß:]*)$/,
      url: q => `/tasks/tag-names?q=${encodeURIComponent(q)}`,
      items: data => (data.tags || []).map(t => ({
        token: t.token,
        // The tag's own colour, but only if it really is one: this goes in
        // through innerHTML, so anything else is dropped rather than trusted.
        html: `<i class="bi bi-tag"${/^#[0-9a-f]{3,8}$/i.test(t.color || "")
                 ? ` style="color: ${t.color}"` : ""}></i> ${escape(t.name)}`,
      })),
    },
  };

  function attach(field, options) {
    const opts = options || {};
    const sources = Object.assign({}, BASE_SOURCES, opts.sources || {});
    const box = document.createElement("div");
    // `qa-assignee-ac` is also the comment @-mention box's class.
    box.className = "qa-assignee-ac d-none";
    if (opts.place) opts.place(box);
    else field.insertAdjacentElement("afterend", box);

    let items = [];     // [{ token, html, pick? }]
    let index = -1;
    let token = null;   // { start, end, query, kind }
    let seq = 0;

    function tokenAtCaret() {
      const pos = field.selectionStart || 0;
      const before = field.value.slice(0, pos);
      for (const [kind, src] of Object.entries(sources)) {
        const m = before.match(src.re);
        if (!m) continue;
        const raw = m[1];
        const query = /^[@+#]"/.test(raw) ? raw.slice(2) : raw.slice(1);
        return { start: pos - raw.length, end: pos, query, kind };
      }
      return null;
    }

    function hide() {
      box.classList.add("d-none");
      box.innerHTML = "";
      items = []; index = -1; token = null;
    }

    function render() {
      if (!items.length) { hide(); return; }
      box.innerHTML = items.map((it, i) =>
        `<button type="button" class="qa-ac-item${i === index ? " active" : ""}" `
        + `data-i="${i}">${it.html}</button>`).join("");
      if (opts.position) opts.position(box, field, token);
      box.classList.remove("d-none");
    }

    async function refresh() {
      const tok = tokenAtCaret();
      if (!tok) { hide(); return; }
      token = tok;
      const src = sources[tok.kind];
      const mine = ++seq;
      if (src.local) {
        items = src.local(tok.query);
        index = -1;
        render();
        return;
      }
      try {
        const resp = await fetch(src.url(tok.query), { credentials: "same-origin" });
        if (!resp.ok || mine !== seq) return;
        items = src.items(await resp.json());
        index = -1;
        render();
      } catch (e) { hide(); }
    }

    function apply(i) {
      const item = items[i];
      if (!item || !token) return;
      const v = field.value;
      if (item.pick) item.pick();
      const replacement = item.token ? item.token + " " : "";
      field.value = v.slice(0, token.start) + replacement + v.slice(token.end);
      const caret = token.start + replacement.length;
      field.setSelectionRange(caret, caret);
      hide();
      if (opts.onApply) opts.onApply();
      field.focus();
    }

    field.addEventListener("input", refresh);
    field.addEventListener("keydown", (ev) => {
      if (box.classList.contains("d-none") || !items.length) return;
      if (ev.key === "ArrowDown") {
        // From "nothing highlighted" (-1) this lands on the first entry.
        ev.preventDefault(); index = (index + 1) % items.length; render();
      } else if (ev.key === "ArrowUp") {
        // ...and this one on the last, rather than one short of it.
        ev.preventDefault(); index = (index <= 0 ? items.length : index) - 1; render();
      } else if (ev.key === "Enter" || ev.key === "Tab") {
        // ⌘/Ctrl+Enter is "save" and is never a completion, even with an
        // entry highlighted: it is the one key that has to work everywhere.
        if (ev.key === "Enter" && (ev.metaKey || ev.ctrlKey)) { hide(); return; }
        // Only once an entry is actually highlighted. Otherwise the key does
        // what the field does with it.
        if (index < 0) return;
        ev.preventDefault(); ev.stopPropagation(); apply(index);
      } else if (ev.key === "Escape") {
        // Closes the list, not a dialog around it: the next Escape does that.
        ev.preventDefault(); ev.stopPropagation(); hide();
      }
    });
    box.addEventListener("mousedown", (ev) => {
      const btn = ev.target.closest(".qa-ac-item");
      if (!btn) return;
      ev.preventDefault();
      apply(parseInt(btn.dataset.i, 10));
    });
    field.addEventListener("blur", () => setTimeout(hide, 120));
    return { hide, box };
  }

  window.CairnTokenComplete = { attach, escape };
})();
