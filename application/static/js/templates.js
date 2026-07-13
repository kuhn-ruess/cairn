/* Template editor — add / delete sub-items in place, no full-page reload.
 *
 * The sub-items list (templates/_subitems.html) posts here and we swap just the
 * #tpl-subitems block with the server-rendered partial. Delegated on document
 * so it survives the background <main> refresh, and so pressing Enter in the
 * add box adds a sub-task inline instead of navigating the whole window.
 */
(function () {
  "use strict";

  function csrf() {
    const el = document.querySelector('input[name="csrf_token"]');
    return el ? el.value : "";
  }

  async function post(url, fd) {
    return fetch(url, {
      method: "POST",
      body: fd,
      credentials: "same-origin",
      headers: { "X-Requested-With": "XMLHttpRequest" },
    });
  }

  function swap(html) {
    const wrap = document.getElementById("tpl-subitems");
    if (!wrap) return;
    const tmp = document.createElement("div");
    tmp.innerHTML = html.trim();
    const fresh = tmp.firstElementChild;
    if (fresh) {
      wrap.replaceWith(fresh);
      // Refocus the add box so you can keep adding sub-tasks.
      const input = document.querySelector("#tpl-subitems .tpl-subtask-input");
      if (input) input.focus();
    }
  }

  // Add a sub-task/section — intercept the form submit (covers Enter + click).
  document.addEventListener("submit", async (ev) => {
    const form = ev.target.closest("form[data-tpl-subtask-form]");
    if (!form) return;
    ev.preventDefault();
    const input = form.querySelector(".tpl-subtask-input");
    if (!input || !input.value.trim()) return;
    const fd = new FormData(form);
    input.disabled = true;
    try {
      const resp = await post(form.action, fd);
      if (resp.ok) swap(await resp.text());
      else input.disabled = false;
    } catch (e) {
      input.disabled = false;
    }
  });

  // Delete a child sub-item, in place.
  document.addEventListener("click", async (ev) => {
    const btn = ev.target.closest(".tpl-child-delete");
    if (!btn) return;
    ev.preventDefault();
    if (!confirm("Delete this item and everything under it?")) return;
    const fd = new FormData();
    fd.append("csrf_token", csrf());
    try {
      const resp = await post(btn.dataset.action, fd);
      if (resp.ok) swap(await resp.text());
    } catch (e) {
      /* leave as-is */
    }
  });
})();
