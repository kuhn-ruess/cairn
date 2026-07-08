/* Upgrade a <select multiple data-tag-toggle> into an OmniFocus-style token
 * field: only the *assigned* tags show, each as a removable colored chip, and
 * you add more by typing in a search box that autocompletes existing tags.
 *
 * The native <select> stays (hidden) as the form's source of truth; we mirror
 * every add/remove onto it and fire `change`, so the normal inspector save
 * picks the selection up — no per-tag AJAX.
 *
 * Exclusive groups: `data-tag-meta` carries each tag's parent + whether that
 * parent allows only one child (`excl`). Adding a tag from such a group
 * instantly drops its already-assigned sibling chip, so the constraint the
 * server enforces on save is visible the moment you pick it.
 *
 * Re-inits on inspector:loaded so it also works for the AJAX-loaded inspector
 * fragment (and after a save swaps the body).
 */
(function () {
  // #rrggbb → same color with an alpha suffix for a soft fill; pass others through.
  function tint(hex, alpha) {
    return /^#[0-9a-fA-F]{6}$/.test(hex) ? hex + alpha : hex;
  }

  function build(sel) {
    if (sel.dataset.tagToggleWired === "1") return;
    sel.dataset.tagToggleWired = "1";
    sel.style.display = "none";

    const meta = {};
    try {
      JSON.parse(sel.dataset.tagMeta || "[]").forEach((m) => { meta[m.name] = m; });
    } catch (e) { /* fall back to plain tokens with no colors/exclusivity */ }
    const colorOf = (name) => (meta[name] && meta[name].color) || "#6c757d";

    // Ancestor names top→immediate-parent, so hierarchy stays readable as a
    // "Parent › Child" path on both chips and suggestions.
    function pathPrefix(name) {
      const out = [];
      let p = meta[name] && meta[name].parent;
      let guard = 0;
      while (p && guard++ < 10) {
        out.unshift(p);
        p = meta[p] && meta[p].parent;
      }
      return out.length ? out.join(" › ") + " › " : "";
    }

    // Fill `el` with a muted ancestor path + the tag name; tooltip = full path.
    function labelInto(el, name) {
      const pre = pathPrefix(name);
      if (pre) {
        const m = document.createElement("span");
        m.className = "text-muted";
        m.textContent = pre;
        el.appendChild(m);
      }
      el.appendChild(document.createTextNode(name));
      el.title = pre + name;
    }

    const opts = Array.from(sel.options);
    const byVal = {};
    opts.forEach((o) => { byVal[o.value] = o; });

    const wrap = document.createElement("div");
    wrap.className = "position-relative";

    const field = document.createElement("div");
    field.className = "form-control form-control-sm d-flex flex-wrap align-items-center gap-1";
    field.style.cssText = "height:auto;min-height:calc(1.5em + .5rem + 2px);cursor:text;";

    const input = document.createElement("input");
    input.type = "text";
    input.className = "flex-grow-1 border-0 p-0";
    input.style.cssText = "outline:none;min-width:6rem;background:transparent;font-size:.8rem;";

    const menu = document.createElement("div");
    menu.className = "border rounded shadow-sm";
    menu.style.cssText =
      "position:absolute;left:0;right:0;top:100%;margin-top:2px;z-index:1056;" +
      "max-height:13rem;overflow:auto;background:var(--gv-bg1,#fff);display:none;";

    let current = []; // option values currently shown in the menu
    let active = -1;   // highlighted menu row

    function chip(o) {
      const color = colorOf(o.value);
      const c = document.createElement("span");
      c.className = "tag-token d-inline-flex align-items-center gap-1";
      c.style.cssText =
        "border:1px solid " + color + ";background:" + tint(color, "2e") + ";" +
        "color:var(--gv-fg1,inherit);border-radius:999px;padding:.05rem .25rem .05rem .45rem;" +
        "font-size:.72rem;line-height:1.5;max-width:100%;";
      const dot = document.createElement("span");
      dot.style.cssText = "width:.45rem;height:.45rem;border-radius:50%;flex:0 0 auto;background:" + color + ";";
      const txt = document.createElement("span");
      labelInto(txt, o.value);
      txt.style.cssText = "overflow:hidden;text-overflow:ellipsis;white-space:nowrap;";
      const x = document.createElement("span");
      x.textContent = "×";
      x.style.cssText = "cursor:pointer;opacity:.55;font-size:.95rem;line-height:1;padding:0 .05rem;";
      x.addEventListener("mouseenter", () => { x.style.opacity = "1"; });
      x.addEventListener("mouseleave", () => { x.style.opacity = ".55"; });
      x.addEventListener("click", (e) => {
        e.stopPropagation();
        o.selected = false;
        render();
        commit();
        input.focus();
        openMenu();
      });
      c.appendChild(dot);
      c.appendChild(txt);
      c.appendChild(x);
      return c;
    }

    function render() {
      Array.from(field.querySelectorAll(".tag-token")).forEach((n) => n.remove());
      opts.filter((o) => o.selected).forEach((o) => field.insertBefore(chip(o), input));
      input.placeholder = field.querySelector(".tag-token") ? "" : "Add label…";
    }

    function add(value) {
      const o = byVal[value];
      if (!o || o.selected) return;
      o.selected = true;
      // Exclusive group: adding a child drops any already-assigned sibling.
      const m = meta[value];
      const pmeta = m && m.parent ? meta[m.parent] : null;
      if (pmeta && pmeta.excl) {
        opts.forEach((s) => {
          if (s === o || !s.selected) return;
          const sm = meta[s.value];
          if (sm && sm.parent === m.parent) s.selected = false;
        });
      }
      input.value = "";
      render();
      commit();
      openMenu();
    }

    function commit() {
      sel.dispatchEvent(new Event("change", { bubbles: true }));
    }

    // Type a name that doesn't exist yet → create the option on the spot and
    // assign it. The view creates the matching Tag on the user at save time.
    function createTag(name) {
      if (!byVal[name]) {
        const o = document.createElement("option");
        o.value = name;
        o.textContent = name;
        sel.appendChild(o);
        opts.push(o);
        byVal[name] = o;
      }
      add(name);
    }

    function openMenu() {
      const q = input.value.trim();
      const ql = q.toLowerCase();
      const matches = opts.filter((o) => !o.selected && (!ql || o.value.toLowerCase().includes(ql)));
      // Offer "Create" when the typed name matches no existing tag exactly.
      const exact = opts.some((o) => o.value.toLowerCase() === ql);
      current = matches.map((o) => ({ value: o.value, create: false }));
      if (q && !exact) current.push({ value: q, create: true });

      active = current.length ? 0 : -1;
      menu.innerHTML = "";
      if (!current.length) { menu.style.display = "none"; return; }
      current.forEach((item, i) => {
        const row = document.createElement("div");
        row.className = "d-flex align-items-center gap-2";
        row.style.cssText = "padding:.28rem .55rem;cursor:pointer;font-size:.78rem;";
        const dot = document.createElement("span");
        dot.style.cssText = "width:.5rem;height:.5rem;border-radius:50%;flex:0 0 auto;background:" + colorOf(item.value) + ";";
        const t = document.createElement("span");
        if (item.create) {
          t.innerHTML = "";
          const lead = document.createElement("span");
          lead.className = "text-muted";
          lead.textContent = "Create ";
          const nm = document.createElement("span");
          nm.textContent = "“" + item.value + "”";
          t.appendChild(lead);
          t.appendChild(nm);
        } else {
          labelInto(t, item.value);
        }
        row.appendChild(dot);
        row.appendChild(t);
        row.addEventListener("mousedown", (e) => {
          e.preventDefault();
          item.create ? createTag(item.value) : add(item.value);
        });
        row.addEventListener("mousemove", () => { active = i; paintMenu(); });
        menu.appendChild(row);
      });
      paintMenu();
      menu.style.display = "block";
    }

    function paintMenu() {
      Array.from(menu.children).forEach((row, i) => {
        row.style.background = i === active ? "var(--gv-bg2,rgba(128,128,128,.18))" : "";
      });
    }

    function closeMenu() { menu.style.display = "none"; active = -1; }

    field.addEventListener("mousedown", (e) => {
      // Click anywhere in the field (but not on a chip's ×) focuses the input.
      if (e.target === field) input.focus();
    });
    input.addEventListener("focus", openMenu);
    input.addEventListener("input", openMenu);
    input.addEventListener("blur", () => setTimeout(closeMenu, 120));
    input.addEventListener("keydown", (e) => {
      if (e.key === "ArrowDown") {
        e.preventDefault();
        if (current.length) { active = (active + 1) % current.length; paintMenu(); }
      } else if (e.key === "ArrowUp") {
        e.preventDefault();
        if (current.length) { active = (active - 1 + current.length) % current.length; paintMenu(); }
      } else if (e.key === "Enter") {
        // Cmd/Ctrl+Enter is the global save chord — let it bubble to the form.
        if (e.metaKey || e.ctrlKey) return;
        // Plain Enter in the label box operates on labels only — never let it
        // bubble to the inspector's form-level Enter=save handler (which would
        // submit the new-task form and create a stray task while picking a tag).
        e.preventDefault();
        e.stopPropagation();
        const item = active >= 0 ? current[active] : null;
        if (item) {
          item.create ? createTag(item.value) : add(item.value);
        }
      } else if (e.key === "Backspace" && !input.value) {
        const sels = opts.filter((o) => o.selected);
        if (sels.length) { sels[sels.length - 1].selected = false; render(); commit(); openMenu(); }
      } else if (e.key === "Escape") {
        closeMenu();
      }
    });

    field.appendChild(input);
    wrap.appendChild(field);
    wrap.appendChild(menu);
    render();
    sel.parentNode.insertBefore(wrap, sel.nextSibling);
  }

  function init() {
    document.querySelectorAll("select[multiple][data-tag-toggle]").forEach(build);
  }

  if (document.readyState !== "loading") init();
  else document.addEventListener("DOMContentLoaded", init);
  document.addEventListener("inspector:loaded", init);
})();
