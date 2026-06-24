/* Upgrade a <select multiple data-tag-toggle> into a click-to-toggle label list.
 *
 * A native multi-select REPLACES its whole selection on a plain click, so
 * assigning a second label silently drops the first unless you hold ⌘/Ctrl.
 * We keep the real <select> (hidden) as the form's source of truth and mirror
 * each click onto it: one click flips just that label on/off, others untouched.
 *
 * Re-inits on inspector:loaded so it also works for the AJAX-loaded inspector
 * fragment (and after a save swaps the body).
 */
(function () {
  function build(sel) {
    if (sel.dataset.tagToggleWired === "1") return;
    sel.dataset.tagToggleWired = "1";
    sel.style.display = "none";

    const opts = Array.from(sel.options);
    const box = document.createElement("div");
    box.className = "border rounded";

    let filter = null;
    if (opts.length > 6) {
      filter = document.createElement("input");
      filter.type = "search";
      filter.className = "form-control form-control-sm border-0 border-bottom rounded-0";
      filter.placeholder = "Search labels…";
      box.appendChild(filter);
    }

    const list = document.createElement("div");
    list.style.maxHeight = "11rem";
    list.style.overflowY = "auto";
    box.appendChild(list);

    opts.forEach((o) => {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "btn btn-sm w-100 text-start d-flex align-items-center gap-2 rounded-0 tag-toggle-opt"
        + (o.selected ? " active" : "");
      const icon = document.createElement("i");
      icon.className = o.selected ? "bi bi-check-square" : "bi bi-square";
      const span = document.createElement("span");
      span.style.whiteSpace = "pre";          // keep the tag-tree indentation
      span.textContent = o.textContent;
      b.appendChild(icon);
      b.appendChild(span);
      b._option = o;
      list.appendChild(b);
    });

    list.addEventListener("click", (e) => {
      const b = e.target.closest(".tag-toggle-opt");
      if (!b) return;
      const o = b._option;
      o.selected = !o.selected;
      b.classList.toggle("active", o.selected);
      b.querySelector("i").className = o.selected ? "bi bi-check-square" : "bi bi-square";
      sel.dispatchEvent(new Event("change", { bubbles: true }));
    });

    if (filter) {
      filter.addEventListener("input", () => {
        const q = filter.value.trim().toLowerCase();
        list.querySelectorAll(".tag-toggle-opt").forEach((b) => {
          b.style.display = (!q || b.textContent.toLowerCase().includes(q)) ? "" : "none";
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
