/* 12 Week Year: ticks, notes, lag measures and the Habitify read swap the
 * cycle body in place (AJAX over POST, so Back stays clean). Delegated on
 * document because the app's background <main> refresh replaces the DOM. */
(function () {
  "use strict";

  function csrf() {
    const el = document.querySelector('input[name="csrf_token"]');
    return el ? el.value : "";
  }

  async function send(url, fd) {
    fd.append("csrf_token", csrf());
    const resp = await fetch(url, {
      method: "POST", body: fd, credentials: "same-origin",
      headers: { "X-CSRFToken": csrf(), "X-Requested-With": "XMLHttpRequest" },
    });
    // 422 is a refused input: the body comes back with the error beside the
    // field and the value still in it, so it is swapped in like a success.
    if (!resp.ok && resp.status !== 422) throw new Error("request failed");
    const box = document.getElementById("twy-body");
    if (box) box.innerHTML = await resp.text();
  }

  document.addEventListener("change", function (ev) {
    const box = ev.target.closest("[data-twy-toggle]");
    if (!box) return;
    const fd = new FormData();
    fd.append("tid", box.dataset.tid);
    fd.append("week", box.dataset.week);
    box.disabled = true;
    send(box.dataset.url, fd).catch(function () {
      box.checked = !box.checked;
      box.disabled = false;
    });
  });

  document.addEventListener("click", function (ev) {
    const btn = ev.target.closest("[data-twy-log]");
    if (!btn) return;
    ev.preventDefault();
    const fd = new FormData();
    fd.append("tid", btn.dataset.tid);
    fd.append("week", btn.dataset.week);
    if (btn.dataset.undo) fd.append("undo", "1");
    btn.disabled = true;
    send(btn.dataset.url, fd).catch(function () { btn.disabled = false; });
  });

  document.addEventListener("submit", function (ev) {
    const form = ev.target.closest("form[data-twy-form]");
    if (!form) return;
    ev.preventDefault();
    form.querySelectorAll("button").forEach(function (b) { b.disabled = true; });
    send(form.action, new FormData(form)).catch(function () {
      form.querySelectorAll("button").forEach(function (b) { b.disabled = false; });
    });
  });
})();
