/* The custom-field editor on a billing project.
 *
 * Rows are added and removed in place, so a project can be given three fields
 * in one go instead of one save per field. The form still posts five parallel
 * lists (cf_key / cf_label / cf_type / cf_default / cf_options) — because a row
 * carries all five together, adding or removing one keeps them aligned, and a
 * row that isn't on screen at save time is simply a field that no longer
 * exists.
 *
 * Delegated on document: the editor lives inside a <details> that may not be
 * open at load, and the page can be swapped wholesale.
 */
(function () {
  function rowOf(el) {
    return el.closest(".cf-row");
  }

  // A dropdown's choices are typed one per line; the default has to be one of
  // them, so it offers them as suggestions and stays in sync while you type.
  let seq = 0;
  function syncChoices(row) {
    const opts = row.querySelector(".cf-options");
    const def = row.querySelector(".cf-default");
    if (!opts || !def) return;
    let list = row.querySelector("datalist");
    if (!list) {
      list = document.createElement("datalist");
      list.id = "cf-choices-" + ++seq;
      row.appendChild(list);
    }
    def.setAttribute("list", list.id);
    const text = opts.value || "";
    const parts = (text.indexOf("\n") >= 0 ? text.split("\n") : text.split(","))
      .map((p) => p.trim())
      .filter(Boolean);
    list.innerHTML = "";
    parts.forEach((p) => {
      const o = document.createElement("option");
      o.value = p;
      list.appendChild(o);
    });
    // Grow with the list so a long set of choices stays readable.
    opts.rows = Math.min(Math.max(parts.length + 1, 2), 12);
  }

  function applyType(row) {
    const type = row.querySelector(".cf-type");
    const opts = row.querySelector(".cf-options");
    if (!type || !opts) return;
    const isSelect = type.value === "select";
    opts.classList.toggle("d-none", !isSelect);
    if (isSelect) syncChoices(row);
  }

  document.addEventListener("click", (ev) => {
    const add = ev.target.closest(".cf-add");
    if (add) {
      ev.preventDefault();
      const form = add.closest("form");
      const tpl = form && form.querySelector(".cf-template");
      const rows = form && form.querySelector(".cf-rows");
      if (!tpl || !rows) return;
      const row = tpl.content.firstElementChild.cloneNode(true);
      rows.appendChild(row);
      applyType(row);
      const label = row.querySelector('input[name="cf_label"]');
      if (label) label.focus();
      return;
    }

    const rm = ev.target.closest(".cf-remove");
    if (rm) {
      ev.preventDefault();
      const row = rowOf(rm);
      if (row) row.remove();
    }
  });

  document.addEventListener("change", (ev) => {
    const type = ev.target.closest(".cf-type");
    if (type) applyType(rowOf(type));
  });

  document.addEventListener("input", (ev) => {
    const opts = ev.target.closest(".cf-options");
    if (opts) syncChoices(rowOf(opts));
  });
})();
