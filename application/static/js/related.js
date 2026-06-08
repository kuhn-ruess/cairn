/* Related items — symmetric "see also" links shown in the inspector.
 *
 * Same shape as checklist.js: document-level delegation (works in both the
 * inline inspector on the detail page and the off-canvas inspector injected
 * after load), plain buttons carrying their endpoint in data-action, and a
 * server-rendered partial swapped in after every add/remove.
 *
 * Add uses a small typeahead backed by the existing /tasks/quickjump JSON
 * endpoint; picking a result POSTs its id to the add endpoint.
 */
(function () {
  function csrfToken() {
    const el = document.querySelector('input[name="csrf_token"]');
    return el ? el.value : "";
  }

  function blockFor(el) {
    return el.closest("#related, .related-block");
  }

  async function post(action, fields, block) {
    if (!action || !block) return;
    const fd = new FormData();
    fd.append("csrf_token", csrfToken());
    for (const [k, v] of Object.entries(fields || {})) fd.append(k, v);
    const resp = await fetch(action, {
      method: "POST",
      headers: { "X-Partial": "1" },
      body: fd,
    });
    if (!resp.ok) throw new Error(resp.status);
    block.innerHTML = await resp.text();
  }

  function closeResults(box) {
    if (box) { box.innerHTML = ""; box.style.display = "none"; }
  }

  function renderResults(box, results, self) {
    box.innerHTML = "";
    const items = (results || []).filter((r) => r.id && r.id !== self);
    if (!items.length) { box.style.display = "none"; return; }
    for (const r of items) {
      const a = document.createElement("button");
      a.type = "button";
      a.className = "list-group-item list-group-item-action related-result";
      a.dataset.id = r.id;
      const sub = r.subtitle ? ` <span class="text-muted small">— ${r.subtitle}</span>` : "";
      a.innerHTML = `<span class="text-truncate">${escapeHtml(r.title)}</span>${sub}`;
      box.appendChild(a);
    }
    box.style.display = "block";
  }

  function escapeHtml(s) {
    const d = document.createElement("div");
    d.textContent = s == null ? "" : s;
    return d.innerHTML;
  }

  // --- typeahead search (debounced) -------------------------------------
  let timer = null;
  async function search(input) {
    const add = input.closest(".related-add");
    if (!add) return;
    const box = add.querySelector(".related-results");
    const q = input.value.trim();
    if (q.length < 2) { closeResults(box); return; }
    try {
      const resp = await fetch(`${add.dataset.search}?q=${encodeURIComponent(q)}`);
      if (!resp.ok) throw new Error(resp.status);
      const data = await resp.json();
      renderResults(box, data.results, add.dataset.self);
    } catch (_) { closeResults(box); }
  }

  document.addEventListener("input", (ev) => {
    const input = ev.target.closest(".related-add-input");
    if (!input) return;
    clearTimeout(timer);
    timer = setTimeout(() => search(input), 200);
  });

  // Pick a search result → link it.
  document.addEventListener("click", (ev) => {
    const res = ev.target.closest(".related-result");
    if (res) {
      ev.preventDefault();
      const add = res.closest(".related-add");
      post(add && add.dataset.action, { related_id: res.dataset.id }, blockFor(res))
        .catch(() => {});
      return;
    }
    const rm = ev.target.closest(".related-remove");
    if (rm) {
      ev.preventDefault();
      post(rm.dataset.action, {}, blockFor(rm)).catch(() => {});
      return;
    }
    // Click outside the results dropdown closes it.
    const openBox = document.querySelector(".related-results[style*='block']");
    if (openBox && !ev.target.closest(".related-add")) closeResults(openBox);
  });

  document.addEventListener("keydown", (ev) => {
    const input = ev.target.closest(".related-add-input");
    if (!input) return;
    const add = input.closest(".related-add");
    const box = add && add.querySelector(".related-results");
    // Enter must NOT submit the surrounding inspector form. Instead pick the
    // first match if the dropdown is open.
    if (ev.key === "Enter") {
      ev.preventDefault();
      const first = box && box.querySelector(".related-result");
      if (first) {
        post(add.dataset.action, { related_id: first.dataset.id }, blockFor(first))
          .catch(() => {});
      }
    } else if (ev.key === "Escape") {
      closeResults(box);
    }
  });
})();
