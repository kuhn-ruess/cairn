/* Upgrade a <select multiple data-tag-toggle> into an OmniFocus-style badge
 * picker: each tag is a rounded, colored chip you click to toggle on/off.
 *
 * A native multi-select REPLACES its whole selection on a plain click, so
 * assigning a second label silently drops the first unless you hold ⌘/Ctrl.
 * We keep the real <select> (hidden) as the form's source of truth and mirror
 * each click onto it: one click flips just that tag, others untouched.
 *
 * Exclusive groups: `data-tag-meta` carries each tag's parent + whether that
 * parent allows only one child (`excl`). Picking a tag from such a group
 * instantly deselects its already-selected siblings, so the constraint the
 * server enforces on save is visible the moment you click — no surprise drop.
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
    } catch (e) { /* fall back to plain toggles with no colors/exclusivity */ }

    const opts = Array.from(sel.options);
    const chips = [];

    const box = document.createElement("div");
    box.className = "border rounded p-2";

    let filter = null;
    if (opts.length > 8) {
      filter = document.createElement("input");
      filter.type = "search";
      filter.className = "form-control form-control-sm mb-2";
      filter.placeholder = "Search labels…";
      box.appendChild(filter);
    }

    const list = document.createElement("div");
    list.className = "d-flex flex-wrap gap-1";
    list.style.maxHeight = "12rem";
    list.style.overflowY = "auto";
    box.appendChild(list);

    function paint(chip) {
      const on = chip._option.selected;
      const color = chip._color;
      chip.style.borderColor = on ? color : "var(--gv-bd, rgba(128,128,128,.32))";
      chip.style.background = on ? tint(color, "2e") : "transparent";
      chip.style.color = on ? "var(--gv-fg1, inherit)" : "var(--gv-fg2, inherit)";
      chip.style.fontWeight = on ? "600" : "400";
    }

    opts.forEach((o) => {
      const m = meta[o.value] || {};
      const b = document.createElement("button");
      b.type = "button";
      b.className = "tag-toggle-opt";
      b.style.cssText =
        "display:inline-flex;align-items:center;gap:.35rem;max-width:100%;" +
        "border:1px solid;border-radius:999px;padding:.12rem .55rem;cursor:pointer;" +
        "font-size:.75rem;line-height:1.5;transition:background .12s,border-color .12s,color .12s;";
      b._option = o;
      b._color = m.color || "#6c757d";

      const dot = document.createElement("span");
      dot.style.cssText = "width:.5rem;height:.5rem;border-radius:50%;flex:0 0 auto;";
      dot.style.background = b._color;

      const span = document.createElement("span");
      // Show the clean tag name (the option's value); the visible label text
      // carries tree indent/markers we don't want inside a pill.
      span.textContent = o.value;
      span.style.cssText = "overflow:hidden;text-overflow:ellipsis;white-space:nowrap;";

      b.appendChild(dot);
      b.appendChild(span);
      paint(b);
      chips.push(b);
      list.appendChild(b);
    });

    list.addEventListener("click", (e) => {
      const chip = e.target.closest(".tag-toggle-opt");
      if (!chip) return;
      const o = chip._option;
      o.selected = !o.selected;

      // Turning a tag ON: if its parent only allows one child, drop any
      // already-selected sibling so the change is visible immediately.
      if (o.selected) {
        const m = meta[o.value];
        const parent = m && m.parent;
        const pmeta = parent ? meta[parent] : null;
        if (pmeta && pmeta.excl) {
          chips.forEach((other) => {
            if (other === chip || !other._option.selected) return;
            const om = meta[other._option.value];
            if (om && om.parent === parent) {
              other._option.selected = false;
              paint(other);
            }
          });
        }
      }

      paint(chip);
      sel.dispatchEvent(new Event("change", { bubbles: true }));
    });

    if (filter) {
      filter.addEventListener("input", () => {
        const q = filter.value.trim().toLowerCase();
        chips.forEach((b) => {
          b.style.display = (!q || b._option.value.toLowerCase().includes(q)) ? "inline-flex" : "none";
        });
      });
    }

    sel.parentNode.insertBefore(box, sel.nextSibling);
  }

  function init() {
    document.querySelectorAll("select[multiple][data-tag-toggle]").forEach(build);
  }

  if (document.readyState !== "loading") init();
  else document.addEventListener("DOMContentLoaded", init);
  document.addEventListener("inspector:loaded", init);
})();
