/* Type-to-find for the billing-project <select> on every capture bar.
 *
 * Picking a project while tracking meant opening a native select and scrolling
 * past every customer's every project — the one moment where you want to type
 * three letters and be done. This puts a button + filter panel in front of the
 * select; the <select> stays the source of truth (time-entries.js reads its
 * value and its options' data-fields), so nothing else changes and the form
 * still works with the panel never opened.
 *
 * Delegated on document, because the day sheet and the task block replace this
 * markup after every log, start and stop.
 */
(function () {
  const OPEN = "tt-pick-open";

  function label(sel) {
    const o = sel.options[sel.selectedIndex];
    return o ? o.textContent.trim() : "Pick a project";
  }

  function box(sel) {
    return sel.closest(".tt-pick");
  }

  function paint(sel) {
    const wrap = box(sel);
    if (wrap) wrap.querySelector(".tt-pick-btn").textContent = label(sel);
  }

  /** Build the panel for one picker, lazily on first open. */
  function build(wrap) {
    if (wrap.querySelector(".tt-pick-menu")) return;
    const sel = wrap.querySelector("select");
    const menu = document.createElement("div");
    menu.className = "tt-pick-menu";
    menu.innerHTML =
      '<input type="search" class="form-control form-control-sm tt-pick-search"' +
      ' placeholder="Type to find any project…" autocomplete="off">' +
      '<div class="tt-pick-list"></div>';
    wrap.appendChild(menu);
    const list = menu.querySelector(".tt-pick-list");
    Array.from(sel.options).forEach((o, i) => {
      const row = document.createElement("button");
      row.type = "button";
      row.className = "tt-pick-opt";
      row.dataset.index = String(i);
      row.textContent = o.textContent.trim();
      list.appendChild(row);
    });
  }

  function filter(wrap, q) {
    const needle = q.trim().toLowerCase();
    let first = null;
    let shown = 0;
    wrap.querySelectorAll(".tt-pick-opt").forEach((row) => {
      // Every word has to appear, so "acme web" finds "Acme › Website" without
      // caring which order they were typed in.
      const hay = row.textContent.toLowerCase();
      const hit = !needle || needle.split(/\s+/).every((w) => hay.includes(w));
      row.hidden = !hit;
      row.classList.remove("active");
      if (hit) {
        shown += 1;
        if (!first) first = row;
      }
    });
    if (first) first.classList.add("active");
    return shown;
  }

  /* No hit is an answer too: the panel used to go silently blank, which reads
   * exactly like a search that broke. One line, and it names what was typed. */
  function empty(wrap, needle) {
    const list = wrap.querySelector(".tt-pick-list");
    let note = list.querySelector(".tt-pick-none");
    if (!needle) {
      if (note) note.remove();
      return;
    }
    if (!note) {
      note = document.createElement("div");
      note.className = "tt-pick-none text-muted small";
      list.appendChild(note);
    }
    note.textContent = needle.length < 2
      ? "Keep typing to search every project…"
      : "No billing project matches “" + needle + "”.";
  }

  /* The page carries a shortlist — the projects this person actually books on.
   * Everything else is asked for, so a team with a thousand projects costs the
   * capture bar nothing until someone goes looking for the thousandth. */
  const timers = new WeakMap();

  function remote(wrap, q) {
    clearTimeout(timers.get(wrap));
    if (q.trim().length < 2) {
      wrap.querySelectorAll(".tt-pick-opt[data-remote]").forEach((r) => r.remove());
      return;
    }
    timers.set(wrap, setTimeout(() => {
      const url = "/time/projects/search?q=" + encodeURIComponent(q.trim());
      fetch(url, { headers: { "X-Requested-With": "fetch" } })
        .then((r) => (r.ok ? r.json() : { projects: [] }))
        .then((data) => {
          // Only answer the question that is still on screen.
          const search = wrap.querySelector(".tt-pick-search");
          if (!search || search.value.trim() !== q.trim()) return;
          const sel = wrap.querySelector("select");
          const list = wrap.querySelector(".tt-pick-list");
          wrap.querySelectorAll(".tt-pick-opt[data-remote]").forEach((r) => r.remove());
          // What still has a row after that — the shortlist. NOT what the
          // <select> holds: an earlier answer for a shorter prefix ("nol"
          // before "nolte") already put its options there, and skipping those
          // would drop exactly the projects being searched for. The options it
          // left stay valid, so they are reused rather than added twice.
          const listed = new Set(Array.from(list.querySelectorAll(".tt-pick-opt"))
            .map((r) => sel.options[Number(r.dataset.index)])
            .filter(Boolean).map((o) => o.value));
          (data.projects || []).forEach((p) => {
            if (listed.has(p.id)) return;
            let idx = Array.from(sel.options).findIndex((o) => o.value === p.id);
            if (idx < 0) {
              // Give the <select> the option too: it stays the source of truth,
              // and the capture code reads the field definitions off it.
              const opt = document.createElement("option");
              opt.value = p.id;
              opt.textContent = p.label;
              opt.dataset.mode = p.mode || "duration";
              opt.dataset.fields = JSON.stringify(p.fields || []);
              sel.appendChild(opt);
              idx = sel.options.length - 1;
            }
            const row = document.createElement("button");
            row.type = "button";
            row.className = "tt-pick-opt";
            row.dataset.remote = "1";
            row.dataset.index = String(idx);
            row.textContent = p.label;
            list.appendChild(row);
          });
          empty(wrap, filter(wrap, search.value) ? "" : q.trim());
        })
        .catch(() => {});
    }, 160));
  }

  function open(wrap) {
    build(wrap);
    wrap.classList.add(OPEN);
    wrap.querySelector(".tt-pick-btn").setAttribute("aria-expanded", "true");
    filter(wrap, "");
    empty(wrap, "");
    const search = wrap.querySelector(".tt-pick-search");
    search.value = "";
    search.focus();
  }

  function close(wrap) {
    wrap.classList.remove(OPEN);
    wrap.querySelector(".tt-pick-btn").setAttribute("aria-expanded", "false");
  }

  function choose(wrap, row) {
    const sel = wrap.querySelector("select");
    sel.selectedIndex = Number(row.dataset.index);
    sel.dispatchEvent(new Event("change", { bubbles: true }));
    paint(sel);
    close(wrap);
    wrap.querySelector(".tt-pick-btn").focus();
  }

  document.addEventListener("click", (ev) => {
    const btn = ev.target.closest(".tt-pick-btn");
    if (btn) {
      ev.preventDefault();
      const wrap = btn.closest(".tt-pick");
      wrap.classList.contains(OPEN) ? close(wrap) : open(wrap);
      return;
    }
    const row = ev.target.closest(".tt-pick-opt");
    if (row) {
      ev.preventDefault();
      choose(row.closest(".tt-pick"), row);
      return;
    }
    // A click anywhere else closes whatever is open.
    document.querySelectorAll("." + OPEN).forEach((w) => {
      if (!w.contains(ev.target)) close(w);
    });
  });

  document.addEventListener("input", (ev) => {
    const search = ev.target.closest(".tt-pick-search");
    if (!search) return;
    const wrap = search.closest(".tt-pick");
    empty(wrap, filter(wrap, search.value) ? "" : search.value.trim());
    remote(wrap, search.value);
  });

  // The keyboard has to reach all of it: arrows walk the matches, Enter takes
  // the highlighted one, Escape gives up without changing anything.
  document.addEventListener("keydown", (ev) => {
    const search = ev.target.closest(".tt-pick-search");
    if (!search) return;
    const wrap = search.closest(".tt-pick");
    const rows = Array.from(wrap.querySelectorAll(".tt-pick-opt"))
      .filter((r) => !r.hidden);
    const at = rows.findIndex((r) => r.classList.contains("active"));
    if (ev.key === "Escape") {
      ev.preventDefault();
      close(wrap);
      wrap.querySelector(".tt-pick-btn").focus();
    } else if (ev.key === "ArrowDown" || ev.key === "ArrowUp") {
      ev.preventDefault();
      const next = rows[Math.min(Math.max(at + (ev.key === "ArrowDown" ? 1 : -1), 0),
                                 rows.length - 1)];
      if (next) {
        rows.forEach((r) => r.classList.remove("active"));
        next.classList.add("active");
        next.scrollIntoView({ block: "nearest" });
      }
    } else if (ev.key === "Enter") {
      ev.preventDefault();
      if (rows[at < 0 ? 0 : at]) choose(wrap, rows[at < 0 ? 0 : at]);
    }
  });

  /** Take over: mark the wrappers so the CSS shows the button and hides the
   *  select. Until this runs the native control is what the user sees, which
   *  is the whole no-JavaScript story. */
  function ready() {
    document.querySelectorAll(".tt-pick").forEach((wrap) => {
      wrap.dataset.ready = "1";
      const sel = wrap.querySelector("select");
      if (sel) paint(sel);
    });
  }

  if (document.readyState !== "loading") ready();
  else document.addEventListener("DOMContentLoaded", ready);

  // The day sheet and the task block replace this markup after every log, so a
  // freshly swapped picker has to be taken over too. Only *added* markup is
  // considered, and only while an untouched picker exists — ready() writes to
  // the DOM itself (the flag, the button's text), and re-running on every
  // mutation would be a loop that pins the tab.
  new MutationObserver((records) => {
    if (!records.some((r) => r.addedNodes.length)) return;
    if (document.querySelector(".tt-pick:not([data-ready])")) ready();
  }).observe(document.body, { childList: true, subtree: true });

  // Keep the button's text right when something else changes the select
  // (the one-click start on a task, a swapped-in day sheet).
  document.addEventListener("change", (ev) => {
    const sel = ev.target.closest(".time-add-project");
    if (sel && box(sel)) paint(sel);
  });
  document.addEventListener("bktodo:refresh-needed", () => setTimeout(ready, 0));
})();
