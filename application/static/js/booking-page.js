/* The public booking page.
 *
 * The server hands out UTC instants and nothing else — every time on this
 * page is formatted here, in the VISITOR's zone, because that is the only
 * place that knows it. The one toggle switches to the host's zone, for the
 * person who would rather see the time the other side will be looking at.
 *
 * Standalone: the public page doesn't load the app bundle, so this file
 * assumes nothing beyond Bootstrap's stylesheet.
 */
(function () {
  "use strict";

  var script = document.currentScript;
  var SLOTS_URL = script.dataset.slotsUrl;
  var HOST_TZ = script.dataset.hostTz || "UTC";
  var HOST_NAME = script.dataset.hostName || "";
  var GUEST_TZ = (function () {
    try { return Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC"; }
    catch (e) { return "UTC"; }
  })();

  var zone = GUEST_TZ;
  var duration = 0;
  var slotsBox = document.getElementById("slots");
  var form = document.getElementById("book-form");
  var tzName = document.getElementById("tz-name");

  function fmt(iso, opts) {
    return new Intl.DateTimeFormat(undefined,
      Object.assign({ timeZone: zone }, opts)).format(new Date(iso));
  }

  function dayKey(iso) {
    // Sort-stable key for "which local day is this instant on".
    return fmt(iso, { year: "numeric", month: "2-digit", day: "2-digit" });
  }

  var days = [];      // [{key, slots:[iso]}] in order, only days with times
  var chosenDay = 0;
  var pageStart = 0;  // first day of the visible week
  var PER_PAGE = 7;

  function render(slots) {
    slotsBox.textContent = "";
    if (!slots.length) {
      var none = document.createElement("div");
      none.className = "text-muted small";
      none.textContent = "No free times in the next weeks. Try a shorter appointment.";
      slotsBox.appendChild(none);
      return;
    }
    // Days first, times second: a fortnight of half-hours is a wall nobody
    // reads, and the visitor already knows which day they want.
    days = [];
    var byKey = {};
    slots.forEach(function (iso) {
      var key = dayKey(iso);
      if (!byKey[key]) { byKey[key] = { key: key, slots: [] }; days.push(byKey[key]); }
      byKey[key].slots.push(iso);
    });
    if (chosenDay >= days.length) { chosenDay = 0; }
    if (pageStart >= days.length) { pageStart = 0; }

    var shown = days.slice(pageStart, pageStart + PER_PAGE);
    var strip = document.createElement("div");
    strip.className = "book-days";
    // A week at a time with arrows at both ends: the strip used to be one
    // long scroller, which on a mouse looks like there is nothing after the
    // edge of the box.
    strip.appendChild(arrow("‹", pageStart > 0, function () {
      pageStart = Math.max(0, pageStart - PER_PAGE);
      chosenDay = pageStart;
      render(slots);
    }));
    shown.forEach(function (day, i) {
      var at = pageStart + i;
      var b = document.createElement("button");
      b.type = "button";
      b.className = "btn btn-sm book-day-btn " +
        (at === chosenDay ? "btn-primary" : "btn-outline-secondary");
      b.innerHTML = "<span class='book-day-wd'>" +
        fmt(day.slots[0], { weekday: "short" }) + "</span>" +
        "<span class='book-day-num'>" + fmt(day.slots[0], { day: "numeric" }) + "</span>" +
        "<span class='book-day-mon'>" + fmt(day.slots[0], { month: "short" }) + "</span>";
      b.addEventListener("click", function () {
        chosenDay = at;
        render(slots);
      });
      strip.appendChild(b);
    });
    strip.appendChild(arrow("›", pageStart + PER_PAGE < days.length, function () {
      pageStart = pageStart + PER_PAGE;
      chosenDay = pageStart;
      render(slots);
    }));
    slotsBox.appendChild(strip);

    if (days.length > PER_PAGE) {
      var count = document.createElement("div");
      count.className = "book-tz mt-1";
      count.textContent = "Days " + (pageStart + 1) + "–" +
        Math.min(pageStart + PER_PAGE, days.length) + " of " + days.length +
        " with free times";
      slotsBox.appendChild(count);
    }

    var head = document.createElement("div");
    head.className = "book-day-name";
    head.textContent = fmt(days[chosenDay].slots[0], {
      weekday: "long", day: "numeric", month: "long"
    });
    slotsBox.appendChild(head);

    var row = document.createElement("div");
    row.className = "book-slots";
    days[chosenDay].slots.forEach(function (iso) {
      var b = document.createElement("button");
      b.type = "button";
      b.className = "btn btn-sm btn-outline-secondary book-slot";
      b.textContent = fmt(iso, { hour: "2-digit", minute: "2-digit" });
      b.addEventListener("click", function () { choose(iso); });
      row.appendChild(b);
    });
    slotsBox.appendChild(row);
  }

  function arrow(glyph, enabled, onClick) {
    var b = document.createElement("button");
    b.type = "button";
    b.className = "btn btn-sm btn-outline-secondary book-day-arrow";
    b.textContent = glyph;
    b.disabled = !enabled;
    b.setAttribute("aria-label", glyph === "‹" ? "Earlier days" : "Later days");
    if (enabled) { b.addEventListener("click", onClick); }
    return b;
  }

  function choose(iso) {
    document.getElementById("start").value = iso;
    document.getElementById("duration").value = String(duration);
    document.getElementById("tz").value = GUEST_TZ;
    var when = fmt(iso, {
      weekday: "long", day: "numeric", month: "long",
      hour: "2-digit", minute: "2-digit"
    });
    document.getElementById("chosen").textContent =
      when + " · " + duration + " min (" + zone + ")";
    form.classList.remove("d-none");
    form.scrollIntoView({ behavior: "smooth", block: "start" });
    document.getElementById("name").focus();
  }

  var current = null;   // the slot list we last loaded, for a zone switch

  function load() {
    chosenDay = 0;
    pageStart = 0;
    slotsBox.textContent = "";
    var loading = document.createElement("div");
    loading.className = "text-muted small";
    loading.textContent = "Loading free times…";
    slotsBox.appendChild(loading);
    form.classList.add("d-none");
    fetch(SLOTS_URL + "?duration=" + encodeURIComponent(duration), {
      headers: { "Accept": "application/json" }
    }).then(function (r) {
      return r.json().then(function (data) { return { ok: r.ok, data: data }; });
    }).then(function (res) {
      if (!res.ok) { throw new Error(res.data.error || "Free times couldn't be loaded."); }
      current = res.data.slots || [];
      render(current);
    }).catch(function (err) {
      slotsBox.textContent = "";
      var box = document.createElement("div");
      box.className = "alert alert-warning py-2 small mb-0";
      box.textContent = err.message;
      slotsBox.appendChild(box);
    });
  }

  function showZone() {
    tzName.textContent = zone === HOST_TZ && HOST_NAME
      ? zone + " (" + HOST_NAME + "'s time)" : zone;
  }

  document.getElementById("durations").addEventListener("click", function (ev) {
    var btn = ev.target.closest("[data-duration]");
    if (!btn) { return; }
    this.querySelectorAll("[data-duration]").forEach(function (b) {
      b.classList.toggle("btn-primary", b === btn);
      b.classList.toggle("btn-outline-secondary", b !== btn);
    });
    duration = parseInt(btn.dataset.duration, 10);
    load();
  });

  document.getElementById("tz-toggle").addEventListener("click", function () {
    zone = zone === GUEST_TZ ? HOST_TZ : GUEST_TZ;
    showZone();
    if (current) { render(current); }
  });

  document.getElementById("cancel").addEventListener("click", function () {
    form.classList.add("d-none");
    slotsBox.scrollIntoView({ behavior: "smooth", block: "start" });
  });

  var first = document.querySelector("#durations [data-duration]");
  duration = first ? parseInt(first.dataset.duration, 10) : 0;
  showZone();
  load();
})();
