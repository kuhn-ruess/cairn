/* Slash commands for the note editor.

   Type "/" at the start of a word and a menu offers the things that are
   tedious to type by hand — a checklist item, a table, a code fence, and above
   all a **link with a target picker**: "/link " then a few letters searches
   your notes, tasks and projects and drops the Markdown link in.

   Attach it to a CodeMirror instance (EasyMDE's) and drive it from the same
   two hooks the [[wiki]] completion uses:

     const slash = CairnSlashMenu.attach(cm);
     cm.on("change", () => slash.check());        // re-read the token
     cm.on("cursorActivity", () => slash.check());
     wrapper.addEventListener("keydown", (e) => { if (slash.handleKey(e)) return; }, true);

   ``handleKey`` returns true when the menu consumed the key, so the caller
   stops there. */
(function () {
  const SEARCH_URL = "/tasks/quickjump";
  const NOTE_URL = "/notes/titles";

  function escapeHtml(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, (c) => (
      { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }

  function two(n) { return String(n).padStart(2, "0"); }

  function today() {
    const d = new Date();
    return `${d.getFullYear()}-${two(d.getMonth() + 1)}-${two(d.getDate())}`;
  }

  // ``insert`` replaces the "/token" with text and leaves the cursor at the
  // end; ``run`` gets the CodeMirror so a command can do something richer.
  const COMMANDS = [
    { key: "todo", label: "Checklist item", hint: "- [ ]", insert: "- [ ] " },
    { key: "link", label: "Link…", hint: "search notes, tasks, projects",
      search: "items" },
    { key: "note", label: "Note link…", hint: "[[wiki link]]", search: "notes" },
    { key: "table", label: "Table", hint: "3 × 2",
      insert: "| Column | Column |\n| --- | --- |\n|  |  |\n" },
    { key: "code", label: "Code block", hint: "``` fence",
      run: (cm, replace) => {
        replace("```\n\n```");
        const cur = cm.getCursor();
        cm.setCursor({ line: cur.line - 1, ch: 0 });
      } },
    { key: "quote", label: "Quote", hint: "> ", insert: "> " },
    { key: "heading", label: "Heading", hint: "## ", insert: "## " },
    { key: "divider", label: "Divider", hint: "---", insert: "---\n" },
    { key: "date", label: "Today's date", hint: today, insert: () => today() },
    { key: "time", label: "Time now", hint: "hh:mm",
      insert: () => { const d = new Date(); return `${two(d.getHours())}:${two(d.getMinutes())}`; } },
  ];

  const ICONS = {
    note: "bi-journal-text", project: "bi-collection",
    folder: "bi-folder", task: "bi-check2-square",
  };

  function attach(cm, opts) {
    opts = opts || {};
    const box = document.createElement("ul");
    box.className = "list-group shadow slash-menu";
    box.style.display = "none";
    document.body.appendChild(box);

    let rows = [];          // [{label, hint, apply()}]
    let active = -1, open = false, timer = null;
    let from = null;        // where the "/" sits, i.e. what gets replaced

    function hide() {
      box.style.display = "none";
      open = false; active = -1; rows = [];
      clearTimeout(timer);
    }

    function render() {
      box.innerHTML = rows.map((r, i) => (
        `<li class="list-group-item list-group-item-action py-1 small d-flex gap-2 ${i === active ? "active" : ""}"
             role="button" data-i="${i}">
           <span class="text-truncate">${r.icon ? `<i class="bi ${r.icon} me-1"></i>` : ""}${escapeHtml(r.label)}</span>
           <span class="ms-auto opacity-75">${escapeHtml(r.hint || "")}</span>
         </li>`)).join("");
    }

    function place() {
      const pos = cm.cursorCoords(true, "page");
      box.style.left = `${pos.left}px`;
      box.style.top = `${pos.bottom + 4}px`;
      box.style.display = "block";
      open = true;
    }

    function show(next) {
      rows = next;
      if (!rows.length) { hide(); return; }
      active = 0;
      render();
      place();
    }

    /** Swap the "/command …" token for ``text``. */
    function replace(text) {
      cm.replaceRange(text, from, cm.getCursor());
      cm.focus();
    }

    function pick(i) {
      const row = rows[i];
      if (!row || row.disabled) return;
      hide();
      row.apply();
    }

    function commandRows(prefix) {
      return COMMANDS
        .filter((c) => c.key.startsWith(prefix))
        .map((c) => ({
          label: c.label,
          hint: typeof c.hint === "function" ? c.hint() : c.hint,
          apply() {
            // A search command has nothing to insert yet — picking it primes
            // the token so the next keystrokes are the query.
            if (c.search) { replace(`/${c.key} `); return; }
            if (c.run) { c.run(cm, replace); return; }
            replace(typeof c.insert === "function" ? c.insert() : c.insert);
          },
        }));
    }

    async function searchRows(cmd, query) {
      if (!query) {
        return [{ label: "Type to search…", hint: "", disabled: true, apply() {} }];
      }
      if (cmd.search === "notes") {
        const data = await fetch(`${NOTE_URL}?q=${encodeURIComponent(query)}`)
          .then((r) => r.json()).catch(() => ({ results: [] }));
        return (data.results || []).map((n) => ({
          label: n.title, hint: "note", icon: ICONS.note,
          apply: () => replace(`[[${n.title}]]`),
        }));
      }
      const url = `${SEARCH_URL}?q=${encodeURIComponent(query)}&kinds=project,folder,task,note`;
      const data = await fetch(url).then((r) => r.json()).catch(() => ({ results: [] }));
      return (data.results || []).map((r) => ({
        label: r.title, hint: r.subtitle || r.kind, icon: ICONS[r.kind],
        apply: () => replace(`[${r.title.replace(/[[\]]/g, "")}](${r.url})`),
      }));
    }

    // mousedown, not click — the editor's blur handler would close us first.
    box.addEventListener("mousedown", (e) => {
      const li = e.target.closest("[data-i]");
      if (li) { e.preventDefault(); pick(parseInt(li.dataset.i, 10)); }
    });

    return {
      hide,

      /** Re-read the token under the cursor and open/close the menu. */
      check() {
        const cur = cm.getCursor();
        const line = cm.getRange({ line: cur.line, ch: 0 }, cur);
        // "/word" — at the start of a line or after a space, so a URL's "//"
        // and a closed "/path/like/this" never trigger it. Everything after
        // the first space is the search query.
        const m = line.match(/(?:^|\s)\/([a-zA-Z]*)( [^/]*)?$/);
        if (!m) { hide(); return; }
        from = { line: cur.line, ch: cur.ch - (m[1].length + (m[2] || "").length + 1) };
        clearTimeout(timer);

        if (m[2] === undefined) {
          show(commandRows(m[1].toLowerCase()));
          return;
        }
        const cmd = COMMANDS.find((c) => c.key === m[1].toLowerCase() && c.search);
        if (!cmd) { hide(); return; }
        const query = m[2].trim();
        // Keep the menu where it is while the query lands.
        timer = setTimeout(async () => show(await searchRows(cmd, query)), 150);
      },

      handleKey(e) {
        if (!open) return false;
        if (e.key === "ArrowDown") { active = (active + 1) % rows.length; render(); }
        else if (e.key === "ArrowUp") { active = (active - 1 + rows.length) % rows.length; render(); }
        else if (e.key === "Enter" || e.key === "Tab") { pick(active); }
        else if (e.key === "Escape") { hide(); }
        else return false;
        e.preventDefault();
        e.stopPropagation();
        return true;
      },
    };
  }

  window.CairnSlashMenu = { attach, COMMANDS };
})();
