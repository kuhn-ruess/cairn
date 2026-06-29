/* Live "now" line for the Today calendar (.cal-now) and the Plan timeline
 * (.plan-now).
 *
 * Both bars are positioned server-side at page load and were never updated
 * client-side, so they drifted out of date — most visibly when the tab sat in
 * the background (browsers throttle/pause setInterval there) and you came back
 * to a stale bar. We recompute their position every 30s AND immediately
 * whenever the tab becomes visible / regains focus, so returning to the tab
 * snaps the bar to the real current time.
 *
 * Loaded globally and document-level: it re-queries the DOM each tick, so it
 * keeps working after a <main> swap (bktodo:refresh-needed) and no-ops on pages
 * that have neither bar.
 */
(function () {
  function pad(n) { return n < 10 ? "0" + n : "" + n; }
  function localDate(d) {
    return d.getFullYear() + "-" + pad(d.getMonth() + 1) + "-" + pad(d.getDate());
  }

  // Plan timeline: one continuous track, --top = minutesSinceMidnight - range0.
  function updatePlan(d) {
    const track = document.getElementById("plan-track");
    const nowEl = document.querySelector(".plan-now");
    if (!track || !nowEl) return;
    // Only the day actually being viewed gets a live bar (the server only
    // renders .plan-now for today, but guard anyway in case of a stale swap).
    if (track.dataset.date && track.dataset.date !== localDate(d)) return;
    const range0 = parseInt(track.dataset.range0, 10) || 0;
    const mins = d.getHours() * 60 + d.getMinutes();
    nowEl.style.setProperty("--top", String(mins - range0));
    const label = nowEl.querySelector(".plan-now-label");
    if (label) label.textContent = pad(d.getHours()) + ":" + pad(d.getMinutes());
    const past = document.querySelector(".plan-past");
    if (past) past.style.setProperty("--dur", String(Math.max(0, mins - range0)));
  }

  // Today calendar: the bar lives inside the current hour's today-column cell;
  // as the hour rolls over we move it into the next hour's cell.
  function updateCal(d) {
    const nowEl = document.querySelector(".cal-now");
    if (!nowEl) return;
    const h = d.getHours(), m = d.getMinutes();
    const target = document.querySelector(
      '.day-hour-slots.is-today-col[data-hour="' + h + '"]');
    if (target && nowEl.parentElement !== target) target.appendChild(nowEl);
    nowEl.style.setProperty("--cal-top-min", String(m));
    const t = nowEl.querySelector(".cal-now-time");
    if (t) t.textContent = pad(h) + ":" + pad(m);
  }

  function tick() {
    const d = new Date();
    updatePlan(d);
    updateCal(d);
  }

  tick();
  setInterval(tick, 30000);
  document.addEventListener("visibilitychange", function () {
    if (!document.hidden) tick();
  });
  window.addEventListener("focus", tick);
  window.addEventListener("pageshow", tick);
  document.addEventListener("bktodo:refresh-needed", function () {
    setTimeout(tick, 0);
  });
})();
