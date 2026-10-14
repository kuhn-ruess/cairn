/* Today "Plan" timeline interactions (structured.app-style day planner).
 *
 * Loaded globally and self-gating: every handler is delegated on `document`
 * and resolves the current #plan-track at fire time. That way it keeps
 * working after the app's background <main> refresh (bktodo:refresh-needed,
 * fired by inspector saves etc.) swaps the timeline DOM out — a previous
 * version bound handlers to the captured element and silently died after
 * any inspector edit until a full reload.
 */
(function () {
  "use strict";
  const SNAP = 15, PX_PER_STEP = 20, STEP = 15;
  let dragId = null, dragEl = null, dragEst = 30, rz = null, qa = null, justDropped = false;

  const fmt = (m) => String(Math.floor(m / 60)).padStart(2, "0") + ":" +
                     String(m % 60).padStart(2, "0");

  function csrf() {
    const el = document.querySelector('input[name="csrf_token"]');
    return el ? el.value : "";
  }
  function postForm(url, data) {
    const fd = new FormData();
    fd.append("csrf_token", csrf());
    for (const k in data) fd.append(k, data[k]);
    return fetch(url, {
      method: "POST", body: fd, credentials: "same-origin",
      headers: { "X-CSRFToken": csrf(), "X-Requested-With": "XMLHttpRequest" },
    });
  }

  // Resolve the live timeline + its scale on demand (survives DOM swaps).
  function ctx() {
    const track = document.getElementById("plan-track");
    if (!track) return null;
    return {
      track,
      lane: track.querySelector(".plan-lane"),
      RANGE0: parseInt(track.dataset.range0, 10) || 0,
      PPM: parseFloat(track.dataset.ppm) || 1.1,
      DATE: track.dataset.date,
    };
  }
  function ensureGhost(track) {
    let g = track.querySelector(".plan-ghost");
    if (!g) {
      g = document.createElement("div");
      g.className = "plan-ghost";
      g.style.display = "none";
      g.innerHTML = '<span class="plan-ghost-label"></span>';
      track.appendChild(g);
    }
    return g;
  }
  function snapAt(c, clientY) {
    const rect = c.track.getBoundingClientRect();
    let m = c.RANGE0 + (clientY - rect.top) / c.PPM;
    m = Math.round(m / SNAP) * SNAP;
    return Math.max(0, Math.min(24 * 60 - SNAP, m));
  }
  function showGhost(c, minutes) {
    const g = ensureGhost(c.track);
    g.style.setProperty("--top", String(minutes - c.RANGE0));
    g.style.setProperty("--dur", String(dragEst));
    g.querySelector(".plan-ghost-label").textContent = fmt(minutes) + "–" + fmt(minutes + dragEst);
    g.style.display = "block";
  }
  function hideGhost(c) {
    const g = c && c.track.querySelector(".plan-ghost");
    if (g) g.style.display = "none";
  }
  function prioClass(el) {
    for (const n of [1, 2, 3]) if (el.classList.contains("plan-prio-" + n)) return " plan-prio-" + n;
    return "";
  }

  // Re-pack overlapping cards into side-by-side columns (mirrors the server).
  function relayoutColumns(c) {
    if (!c || !c.lane) return;
    const items = Array.from(c.lane.querySelectorAll(".plan-card")).map((el) => {
      const start = c.RANGE0 + (parseFloat(el.style.getPropertyValue("--top")) || 0);
      const dur = parseFloat(el.style.getPropertyValue("--dur")) || 30;
      return { el, start, end: start + dur };
    }).sort((a, b) => a.start - b.start || a.end - b.end);
    let cluster = [], clusterEnd = null;
    const flush = (group) => {
      const colEnds = [];
      group.forEach((it) => {
        let col = colEnds.findIndex((e) => e <= it.start);
        if (col === -1) { col = colEnds.length; colEnds.push(it.end); }
        else colEnds[col] = it.end;
        it.col = col;
      });
      group.forEach((it) => {
        it.el.style.setProperty("--col", String(it.col));
        it.el.style.setProperty("--cols", String(colEnds.length));
      });
    };
    items.forEach((it) => {
      if (cluster.length && it.start >= clusterEnd) { flush(cluster); cluster = []; clusterEnd = null; }
      cluster.push(it);
      clusterEnd = clusterEnd === null ? it.end : Math.max(clusterEnd, it.end);
    });
    if (cluster.length) flush(cluster);
  }

  function setCardTime(card, c, minutes, est) {
    card.style.setProperty("--col", "0");
    card.style.setProperty("--cols", "1");
    card.style.setProperty("--dur", String(est));
    card.style.setProperty("--top", String(minutes - c.RANGE0));
    const meta = card.querySelector(".plan-card-meta");
    if (meta) meta.innerHTML = fmt(minutes) + "–" + fmt(minutes + est) +
      ' · <span class="plan-card-dur">' + est + "m</span>";
  }
  // Move an existing card, or convert a rail item into a fresh timeline card.
  function placeOnTimeline(el, c, minutes, est) {
    let card = el;
    if (!el.classList.contains("plan-card")) {
      const link = el.querySelector(".plan-rail-link");
      card = document.createElement("div");
      card.className = "plan-card plan-card-task" + prioClass(el);
      card.setAttribute("draggable", "true");
      card.dataset.taskId = el.dataset.taskId;
      card.dataset.est = String(est);
      if (el.dataset.inspectorUrl) card.dataset.inspectorUrl = el.dataset.inspectorUrl;
      if (el.dataset.inspectorTitle) card.dataset.inspectorTitle = el.dataset.inspectorTitle;
      card.innerHTML =
        '<button type="button" class="plan-ico plan-ico-task plan-complete" title="Mark done"><i class="bi bi-circle"></i></button>' +
        '<div class="plan-card-body"><div class="plan-card-title"><a></a></div>' +
        '<div class="plan-card-meta"></div></div>' +
        '<span class="plan-card-resize" title="Drag to change duration"></span>';
      const a = card.querySelector(".plan-card-title a");
      a.setAttribute("href", link ? link.getAttribute("href") : "#");
      a.textContent = link ? link.textContent.trim() : "";
      c.lane.appendChild(card);
      el.remove();
    }
    setCardTime(card, c, minutes, est);
    return card;
  }
  function cardToRail(card, rail) {
    const link = card.querySelector(".plan-card-title a");
    const est = card.dataset.est || "30";
    const item = document.createElement("div");
    item.className = "plan-rail-item plan-card-task" + prioClass(card);
    item.setAttribute("draggable", "true");
    item.dataset.taskId = card.dataset.taskId;
    item.dataset.est = est;
    if (card.dataset.inspectorUrl) item.dataset.inspectorUrl = card.dataset.inspectorUrl;
    if (card.dataset.inspectorTitle) item.dataset.inspectorTitle = card.dataset.inspectorTitle;
    item.innerHTML = '<i class="bi bi-grip-vertical plan-grip"></i>' +
      '<a class="plan-rail-link"></a><span class="plan-rail-dur">' + est + "m</span>";
    const a = item.querySelector(".plan-rail-link");
    a.setAttribute("href", link ? link.getAttribute("href") : "#");
    a.textContent = link ? link.textContent.trim() : "";
    rail.appendChild(item);
    card.remove();
  }
  function newTaskCard(c, id, title, href, est, minutes) {
    const card = document.createElement("div");
    card.className = "plan-card plan-card-task";
    card.setAttribute("draggable", "true");
    card.dataset.taskId = id;
    card.dataset.est = String(est);
    card.innerHTML =
      '<button type="button" class="plan-ico plan-ico-task plan-complete" title="Mark done"><i class="bi bi-circle"></i></button>' +
      '<div class="plan-card-body"><div class="plan-card-title"><a></a></div>' +
      '<div class="plan-card-meta"></div></div>' +
      '<span class="plan-card-resize" title="Drag to change duration"></span>';
    const a = card.querySelector(".plan-card-title a");
    a.setAttribute("href", href || "#"); a.textContent = title;
    c.lane.appendChild(card);
    setCardTime(card, c, minutes, est);
    return card;
  }

  // ---------- drag (HTML5 DnD), delegated on document ----------
  document.addEventListener("dragstart", (ev) => {
    const p = ev.target.closest && ev.target.closest(".plan-card-task[data-task-id]");
    if (!p || !document.getElementById("plan-track")) return;
    dragId = p.dataset.taskId; dragEl = p;
    dragEst = parseInt(p.dataset.est || "30", 10) || 30;
    ev.dataTransfer.effectAllowed = "move";
    ev.dataTransfer.setData("text/plain", dragId);
    p.classList.add("plan-card-dragging");
  });
  document.addEventListener("dragend", (ev) => {
    const p = ev.target.closest && ev.target.closest(".plan-card-task");
    if (p) p.classList.remove("plan-card-dragging");
    const c = ctx();
    if (c) { c.track.classList.remove("is-drop-over"); hideGhost(c); }
    const bl = document.getElementById("plan-backlog");
    if (bl) bl.classList.remove("is-drop-over");
    dragId = null; dragEl = null;
  });
  document.addEventListener("dragover", (ev) => {
    if (!dragId) return;
    const c = ctx(); if (!c) return;
    const overTrack = ev.target.closest("#plan-track");
    const overBacklog = ev.target.closest("#plan-backlog");
    if (overTrack) {
      ev.preventDefault();
      ev.dataTransfer.dropEffect = "move";
      c.track.classList.add("is-drop-over");
      showGhost(c, snapAt(c, ev.clientY));
      if (overBacklogEl()) overBacklogEl().classList.remove("is-drop-over");
    } else if (overBacklog && dragEl && dragEl.classList.contains("plan-card")) {
      ev.preventDefault();
      ev.dataTransfer.dropEffect = "move";
      overBacklog.classList.add("is-drop-over");
      c.track.classList.remove("is-drop-over"); hideGhost(c);
    }
  });
  function overBacklogEl() { return document.getElementById("plan-backlog"); }
  document.addEventListener("dragleave", (ev) => {
    const c = ctx();
    const tr = ev.target.closest("#plan-track");
    if (tr && c && !tr.contains(ev.relatedTarget)) { tr.classList.remove("is-drop-over"); hideGhost(c); }
    const bl = ev.target.closest("#plan-backlog");
    if (bl && !bl.contains(ev.relatedTarget)) bl.classList.remove("is-drop-over");
  });
  document.addEventListener("drop", (ev) => {
    if (!dragId) return;
    const c = ctx(); if (!c) return;
    const overTrack = ev.target.closest("#plan-track");
    const overBacklog = ev.target.closest("#plan-backlog");
    if (overTrack) {
      ev.preventDefault();
      c.track.classList.remove("is-drop-over"); hideGhost(c);
      const minutes = snapAt(c, ev.clientY);
      const id = dragId, el = dragEl, est = dragEst;
      dragId = null; dragEl = null;
      if (el) el.classList.remove("plan-card-dragging");
      placeOnTimeline(el, c, minutes, est);
      relayoutColumns(c);
      justDropped = true; setTimeout(() => { justDropped = false; }, 60);
      postForm(`/tasks/${id}/schedule`, {
        date: c.DATE, hour: String(Math.floor(minutes / 60)), minute: String(minutes % 60),
      }).then((r) => { if (!r.ok) throw new Error(r.status); })
        .catch(() => window.location.reload());
    } else if (overBacklog && dragEl && dragEl.classList.contains("plan-card")) {
      ev.preventDefault();
      overBacklog.classList.remove("is-drop-over");
      const id = dragId, el = dragEl;
      dragId = null; dragEl = null;
      cardToRail(el, overBacklog);
      relayoutColumns(c);
      // Pass the day: the pill stays ON this day without a time, which is
      // what the rail shows. Clearing the date outright took the task off the
      // plate, so the pill was gone again after the next render.
      postForm(`/tasks/${id}/unschedule`, { date: c.DATE })
        .then((r) => { if (!r.ok) throw new Error(r.status); })
        .catch(() => window.location.reload());
    }
  });

  // ---------- resize handle → estimated_minutes ----------
  document.addEventListener("mousedown", (ev) => {
    const h = ev.target.closest && ev.target.closest(".plan-card-resize");
    if (!h || !document.getElementById("plan-track")) return;
    const pill = h.closest(".plan-card-task");
    if (!pill) return;
    ev.preventDefault(); ev.stopPropagation();
    pill.setAttribute("draggable", "false");
    rz = { pill, startY: ev.clientY, start: parseInt(pill.dataset.est || "30", 10) || 30,
           durEl: pill.querySelector(".plan-card-dur") };
    document.body.classList.add("is-resizing-cal");
  });
  document.addEventListener("mousemove", (ev) => {
    if (!rz) return;
    const steps = Math.round((ev.clientY - rz.startY) / PX_PER_STEP);
    const next = Math.max(STEP, Math.min(1440, rz.start + steps * STEP));
    if (rz.durEl) rz.durEl.textContent = next + "m";
    rz.pill.dataset.est = String(next);
    rz.pill.style.setProperty("--dur", String(next));
  });
  document.addEventListener("mouseup", async () => {
    if (!rz) return;
    const pill = rz.pill, start = rz.start;
    const minutes = parseInt(pill.dataset.est || "30", 10) || 30;
    rz = null;
    pill.setAttribute("draggable", "true");
    document.body.classList.remove("is-resizing-cal");
    if (minutes === start) return;
    relayoutColumns(ctx());
    try {
      const r = await postForm(`/tasks/${pill.dataset.taskId}/duration`, { minutes: String(minutes) });
      if (!r.ok) alert("Resize failed (" + r.status + ")");
    } catch (e) { alert("Resize failed (network)."); }
  });

  // ---------- complete / reopen from the timeline (click the check chip) ----
  // Done tasks STAY on the timeline, struck through — the chip toggles state
  // in place instead of removing the card.
  document.addEventListener("click", (ev) => {
    const chip = ev.target.closest && ev.target.closest(".plan-complete");
    if (!chip || !document.getElementById("plan-track")) return;
    ev.preventDefault(); ev.stopPropagation();
    const card = chip.closest("[data-task-id]");
    if (!card) return;
    const id = card.dataset.taskId;
    // A quick plan step: tick off the step, not the task. Its plan date goes
    // with it, so the card leaves the track; the refresh redraws the day.
    if (chip.hasAttribute("data-quick-plan-done")) {
      chip.disabled = true;
      postForm(`/tasks/${id}/quick-plan`, { when: "done" })
        .then((r) => {
          if (!r.ok) throw new Error(r.status);
          if (window.bktodoRefreshMain) window.bktodoRefreshMain();
          else window.location.reload();
        })
        .catch(() => window.location.reload());
      return;
    }
    const wasDone = card.classList.contains("is-done");
    card.classList.toggle("is-done", !wasDone);
    card.setAttribute("draggable", wasDone ? "true" : "false");
    if (!wasDone) card.classList.remove("plan-card-current");
    const icon = chip.querySelector("i");
    if (icon) icon.className = wasDone ? "bi bi-circle" : "bi bi-check-circle-fill text-success";
    chip.title = wasDone ? "Mark done" : "Reopen";
    postForm(`/tasks/${id}/${wasDone ? "reopen" : "complete"}`, {})
      .then((r) => { if (!r.ok) throw new Error(r.status); })
      .catch(() => window.location.reload());
  });

  // ---------- quick-add: click an empty slot ----------
  function closeQuickAdd() {
    if (!qa) return;
    const el = qa; qa = null;           // null first → re-entrancy safe
    if (el.parentNode) el.remove();
  }
  function openQuickAdd(c, minutes) {
    closeQuickAdd();
    const input = document.createElement("input");
    qa = input;
    input.type = "text";
    input.className = "plan-quickadd";
    input.placeholder = "New task at " + fmt(minutes) + " — Enter to add, Esc to cancel";
    input.style.setProperty("--top", String(minutes - c.RANGE0));
    input.dataset.min = String(minutes);
    c.lane.appendChild(input);
    input.focus();
    input.addEventListener("keydown", (e) => {
      if (e.key === "Escape") { e.preventDefault(); closeQuickAdd(); }
      else if (e.key === "Enter") { e.preventDefault(); submitQuickAdd(); }
    });
    // Only auto-close if THIS input is still the active one and empty.
    input.addEventListener("blur", () => { if (qa === input && !input.value.trim()) closeQuickAdd(); });
  }
  async function submitQuickAdd() {
    if (!qa) return;
    const c = ctx();
    const text = qa.value.trim();
    const minutes = parseInt(qa.dataset.min, 10) || (c ? c.RANGE0 : 0);
    if (!text || !c) { closeQuickAdd(); return; }
    const input = qa; qa = null; input.disabled = true;
    try {
      const r = await postForm("/tasks/plan-create", {
        text, date: c.DATE, hour: String(Math.floor(minutes / 60)),
        minute: String(minutes % 60), minutes: "30",
      });
      const j = await r.json().catch(() => ({}));
      input.remove();
      if (!r.ok) { alert(j.error || ("Create failed (" + r.status + ")")); return; }
      newTaskCard(c, j.id, j.title, j.detail_url, j.minutes || 30, minutes);
      relayoutColumns(c);
    } catch (e) { input.remove(); alert("Create failed (network)."); }
  }
  document.addEventListener("click", (ev) => {
    if (justDropped) return;
    const track = ev.target.closest("#plan-track");
    if (!track) return;
    if (ev.target.closest(".plan-card") || ev.target.closest(".plan-quickadd") ||
        ev.target.closest("a") || ev.target.closest("button")) return;
    const c = ctx(); if (!c) return;
    openQuickAdd(c, snapAt(c, ev.clientY));
  });

  // ---------- Plan my day (delegated) ----------
  async function runAutoPlan(btn, body, busyLabel) {
    const orig = btn.innerHTML;
    btn.disabled = true;
    btn.innerHTML = '<i class="bi bi-hourglass-split"></i> ' + busyLabel;
    try {
      const r = await postForm("/tasks/auto-plan", body);
      const j = await r.json().catch(() => ({}));
      if (!r.ok) { alert("Auto-plan failed (" + r.status + ")"); btn.disabled = false; btn.innerHTML = orig; return; }
      // What didn't fit is NOT gone — it sits in Due. Say that, and say
      // plainly when there was no day left to plan into at all.
      let msg = `Planned ${j.planned} task(s).`;
      if (j.day_over) {
        msg = `Nothing re-planned — the working day (${j.window}) is over.`;
      } else {
        const left = [];
        if (j.skipped) left.push(`${j.skipped} left in Due`);
        if (j.next_left) left.push(`${j.next_left} left in Next actions`);
        if (left.length) msg = `Planned ${j.planned}; ${left.join(", ")} (no room before the day ends).`;
      }
      if (j.carried) msg = `Brought ${j.carried} unfinished task(s) onto today. ` + msg;
      sessionStorage.setItem("plan_toast", msg);
      window.location.reload();
    } catch (e) { alert("Auto-plan failed (network)."); btn.disabled = false; btn.innerHTML = orig; }
  }
  document.addEventListener("click", (ev) => {
    const auto = ev.target.closest("#plan-auto-btn");
    if (auto) { runAutoPlan(auto, { date: auto.dataset.date }, "Planning…"); return; }
  });

  // ---------- per-render setup: centre on "now" + surface toast ----------
  function mount() {
    const c = ctx();
    if (!c) return;
    const nowEl = c.track.querySelector(".plan-now");
    if (nowEl && !c.track.dataset.scrolled) {
      nowEl.scrollIntoView({ block: "center" });
      c.track.dataset.scrolled = "1";
    }
    const toast = sessionStorage.getItem("plan_toast");
    if (toast) {
      sessionStorage.removeItem("plan_toast");
      const el = document.createElement("div");
      el.className = "plan-toast";
      el.textContent = toast;
      document.body.appendChild(el);
      requestAnimationFrame(() => el.classList.add("show"));
      setTimeout(() => el.remove(), 4000);
    }
  }
  if (document.readyState !== "loading") mount();
  else document.addEventListener("DOMContentLoaded", mount);
  // Re-centre after the app swaps <main> (inspector save etc.).
  document.addEventListener("bktodo:refresh-needed", () => setTimeout(mount, 0));
})();
