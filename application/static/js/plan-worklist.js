/* The Plan view's work list (tasks/_plan_worklist.html): the common edits,
 * made on the card itself.
 *
 * Delegated on document, like plan-view.js and tag-remove.js: <main> is
 * swapped wholesale after every inspector save (bktodo:refresh-needed), and
 * anything bound to the cards would be gone after the first one.
 *
 * Every edit goes to an endpoint the rest of the app already uses (complete,
 * schedule / unschedule, quick-edit, note, comment), and updates its
 * own card in place. Nothing here re-renders the list: a card you are halfway
 * through writing a note on must not be redrawn because you ticked another.
 * The one exception is adding a label, which asks for a background refresh of
 * <main> (the chip's colour and a label rule's side effects live server side),
 * and only while no note or comment on the page holds unsaved text.
 */
(function () {
  "use strict";

  function csrf() {
    const el = document.querySelector('input[name="csrf_token"]');
    return el ? el.value : "";
  }
  function post(url, data, headers) {
    const fd = data instanceof FormData ? data : new FormData();
    if (!(data instanceof FormData)) for (const k in data) fd.append(k, data[k]);
    if (!fd.has("csrf_token")) fd.append("csrf_token", csrf());
    return fetch(url, {
      method: "POST", body: fd, credentials: "same-origin",
      headers: Object.assign({ "X-CSRFToken": csrf(), "X-Requested-With": "XMLHttpRequest" },
                             headers || {}),
    }).then((r) => { if (!r.ok) throw new Error(r.status); return r; });
  }
  function flash(card, msg, bad) {
    let el = card.querySelector(".wl-flash");
    if (!el) {
      el = document.createElement("div");
      el.className = "wl-flash small";
      card.querySelector(".wl-head").after(el);
    }
    el.textContent = msg;
    el.classList.toggle("text-danger", !!bad);
    el.classList.toggle("text-muted", !bad);
    clearTimeout(el._t);
    el._t = setTimeout(() => el.remove(), 3500);
  }
  const cardOf = (el) => el.closest(".wl-card");
  const inList = (el) => el && el.closest && el.closest("#plan-worklist");

  // Unsaved text anywhere in the list: a note editor that is open and changed,
  // or a comment box with something typed into it.
  function dirty() {
    const root = document.getElementById("plan-worklist");
    if (!root) return false;
    for (const f of root.querySelectorAll(".wl-note-form:not(.d-none) textarea")) {
      if (f.value !== f.defaultValue) return true;
    }
    for (const t of root.querySelectorAll(".wl-comment-form textarea")) {
      if (t.value.trim()) return true;
    }
    return false;
  }
  function refreshSoon() {
    if (!dirty() && window.bktodoRefreshMain) window.bktodoRefreshMain();
  }

  // ---- done ----------------------------------------------------------------
  // The list is what is still to do, so a ticked card leaves it: fade, fold,
  // remove, the group's count follows and an emptied group goes with it (the
  // same as a task row in every other list, which has no undo either; Reopen
  // is in the inspector and on the timeline, which still shows the task).
  document.addEventListener("click", (ev) => {
    const btn = ev.target.closest && ev.target.closest(".wl-check");
    if (!btn || !inList(btn)) return;
    ev.preventDefault();
    const card = cardOf(btn);
    btn.disabled = true;
    post(btn.dataset.completeUrl, {})
      .then(() => {
        card.style.maxHeight = card.offsetHeight + "px";
        void card.offsetHeight;  // start the fold from the card's own height
        card.classList.add("wl-leaving");
        setTimeout(() => dropCard(card), 220);
      })
      .catch(() => {
        btn.disabled = false;
        flash(card, "Couldn't save, try again.", true);
      });
  });
  function dropCard(card) {
    const group = card.closest(".wl-group");
    card.remove();
    if (group) {
      const left = group.querySelectorAll(".wl-card").length;
      const count = group.querySelector(".plan-rail-count");
      if (count) count.textContent = left;
      if (!left) group.remove();
    }
    const root = document.getElementById("plan-worklist");
    if (root && !root.querySelector(".wl-card")) {
      const empty = root.querySelector(".wl-empty");
      if (empty) empty.classList.remove("d-none");
    }
  }

  // ---- plan date + time, due date -----------------------------------------
  document.addEventListener("change", (ev) => {
    const el = ev.target;
    if (!inList(el)) return;
    const card = cardOf(el);
    if (el.classList.contains("wl-plan-date") || el.classList.contains("wl-plan-time")) {
      const dateIn = card.querySelector(".wl-plan-date");
      const timeIn = card.querySelector(".wl-plan-time");
      const date = dateIn.value;
      const time = timeIn.value;
      let req;
      if (!date) {
        // No day at all: off the plan (same call as dragging it off the day).
        timeIn.value = "";
        req = post(dateIn.dataset.unscheduleUrl, {});
      } else if (!time) {
        // A day without a time: stays on that day, in the Due part.
        req = post(dateIn.dataset.unscheduleUrl, { date });
      } else {
        const [h, m] = time.split(":");
        req = post(dateIn.dataset.scheduleUrl, { date, hour: h, minute: m });
      }
      req.then(() => {
        const moved = date !== document.getElementById("plan-worklist").dataset.date;
        flash(card, moved ? "Planned for another day. It leaves this list on the next reload."
                          : "Saved.");
      }).catch(() => flash(card, "Couldn't save the plan date.", true));
      return;
    }
    if (el.classList.contains("wl-due")) {
      post(el.dataset.url, { due_date: el.value })
        .then(() => flash(card, "Due date saved."))
        .catch(() => flash(card, "Couldn't save the due date.", true));
    }
  });

  // ---- add a label ---------------------------------------------------------
  function addTag(input) {
    const name = input.value.trim();
    if (!name) return;
    const card = cardOf(input);
    let tags = [];
    try { tags = JSON.parse(input.dataset.tags || "[]"); } catch (e) { tags = []; }
    if (tags.includes(name)) { input.value = ""; return; }
    const fd = new FormData();
    fd.append("tags_submitted", "1");
    tags.concat([name]).forEach((t) => fd.append("tags", t));
    input.disabled = true;
    post(input.dataset.url, fd)
      .then((r) => r.json())
      .then((data) => {
        input.dataset.tags = JSON.stringify(data.tags || []);
        input.value = "";
        flash(card, "Label added.");
        refreshSoon();
      })
      .catch(() => flash(card, "Couldn't add the label.", true))
      .finally(() => { input.disabled = false; });
  }
  document.addEventListener("keydown", (ev) => {
    const el = ev.target;
    if (ev.key !== "Enter" || !el.classList || !el.classList.contains("wl-tag-add") || !inList(el)) return;
    ev.preventDefault();
    addTag(el);
  });

  // ---- note: show more, edit in place -------------------------------------
  document.addEventListener("click", (ev) => {
    const t = ev.target.closest && ev.target;
    if (!t || !inList(t)) return;
    const more = t.closest(".wl-note-more");
    if (more) {
      const note = cardOf(more).querySelector(".wl-note");
      const open = note.classList.toggle("is-open");
      more.textContent = open ? "Show less" : "Show more";
      return;
    }
    const edit = t.closest(".wl-note-edit-btn");
    const cancel = t.closest(".wl-note-cancel");
    if (!edit && !cancel) return;
    const wrap = (edit || cancel).closest(".wl-note-wrap");
    const form = wrap.querySelector(".wl-note-form");
    const ta = form.querySelector("textarea");
    if (cancel) ta.value = ta.defaultValue;
    form.classList.toggle("d-none", !!cancel);
    wrap.querySelector(".wl-note").classList.toggle("d-none", !cancel);
    const m = wrap.querySelector(".wl-note-more");
    if (m) m.classList.toggle("d-none", !cancel);
    wrap.querySelector(".wl-note-edit-btn").classList.toggle("d-none", !cancel);
    if (edit) ta.focus();
  });

  document.addEventListener("submit", (ev) => {
    const form = ev.target;
    if (!inList(form)) return;
    if (form.classList.contains("wl-note-form")) {
      ev.preventDefault();
      const wrap = form.closest(".wl-note-wrap");
      const ta = form.querySelector("textarea");
      post(form.action, new FormData(form), { "X-Partial": "1" })
        .then((r) => r.text())
        .then((html) => {
          const view = wrap.querySelector(".wl-note");
          view.innerHTML = html;
          ta.defaultValue = ta.value;
          form.querySelector(".wl-note-cancel").click();
        })
        .catch(() => flash(cardOf(form), "Couldn't save the note.", true));
      return;
    }
    if (form.classList.contains("wl-comment-form")) {
      ev.preventDefault();
      const ta = form.querySelector("textarea");
      if (!ta.value.trim()) return;
      post(form.action, new FormData(form), { "X-Inspector-Request": "1" })
        .then((r) => r.json())
        .then((data) => {
          if (!data.ok) throw new Error(data.error || "refused");
          if (data.comment) {
            const list = form.parentElement.querySelector(".wl-comment-list");
            const row = document.createElement("div");
            row.className = "wl-comment";
            const head = document.createElement("div");
            head.className = "wl-comment-head";
            const who = document.createElement("strong");
            who.textContent = data.author;
            const when = document.createElement("span");
            when.className = "text-muted";
            when.textContent = "just now";
            head.append(who, " ", when);
            const body = document.createElement("div");
            body.className = "md-body";
            body.innerHTML = data.body_html;  // rendered + sanitised server side
            row.append(head, body);
            list.appendChild(row);
          }
          ta.value = "";
        })
        .catch(() => flash(cardOf(form), "Couldn't post the comment.", true));
    }
  });

  // ---- time tracking -------------------------------------------------------
  // The card shows the tracked total and the stopwatch; start / stop / log are
  // time-entries.js's own handlers (the card's section is a .time-block, so
  // they answer with the inspector's partial and swap it in). What lives here
  // is opening that partial on demand, and keeping the OTHER cards honest:
  // starting a timer on one card stops the one running on another, and that
  // card must not keep offering a Stop that would now stop the wrong task.
  function loadTimeBlock(sec) {
    return fetch(sec.dataset.blockUrl, {
      credentials: "same-origin", headers: { "X-Requested-With": "XMLHttpRequest" },
    }).then((r) => { if (!r.ok) throw new Error(r.status); return r.text(); })
      .then((html) => { sec.innerHTML = html; sec.classList.add("is-open"); });
  }
  document.addEventListener("click", (ev) => {
    const btn = ev.target.closest && ev.target.closest(".wl-time-open");
    if (!btn || !inList(btn)) return;
    ev.preventDefault();
    const sec = btn.closest(".wl-time");
    btn.disabled = true;
    loadTimeBlock(sec).catch(() => {
      btn.disabled = false;
      flash(cardOf(sec), "Couldn't load the time tracking.", true);
    });
  });
  document.addEventListener("tt:timer-changed", (ev) => {
    const running = (ev.detail && ev.detail.timerItem) || "";
    const answered = ev.detail && ev.detail.block;
    document.querySelectorAll("#plan-worklist .wl-time").forEach((sec) => {
      if (sec === answered) return;  // just re-rendered by the answer itself
      const showsStop = !!sec.querySelector(".tt-timer-stop");
      if (showsStop !== (sec.dataset.taskId === running)) {
        loadTimeBlock(sec).catch(() => {});
      }
    });
  });

  // Enter posts a comment, Shift+Enter is a new line.
  document.addEventListener("keydown", (ev) => {
    const el = ev.target;
    if (ev.key !== "Enter" || ev.shiftKey || !el.closest || !el.closest(".wl-comment-form") || !inList(el)) return;
    ev.preventDefault();
    el.form.requestSubmit();
  });
})();
