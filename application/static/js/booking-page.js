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

  function pad(n) { return (n < 10 ? "0" : "") + n; }

  function dayKey(iso) {
    // "YYYY-MM-DD" for the day this instant falls on IN THE SHOWN ZONE.
    // Canonical rather than locale-formatted, because it is also sorted,
    // sliced into a month and compared against today.
    var got = {};
    new Intl.DateTimeFormat("en-US", {
      timeZone: zone, year: "numeric", month: "2-digit", day: "2-digit"
    }).formatToParts(new Date(iso)).forEach(function (part) {
      got[part.type] = part.value;
    });
    return got.year + "-" + got.month + "-" + got.day;
  }

  function monthOf(key) { return key.slice(0, 7); }

  function monthShift(ym, delta) {
    var y = +ym.slice(0, 4);
    var m = +ym.slice(5, 7) - 1 + delta;
    y += Math.floor(m / 12);
    return y + "-" + pad(((m % 12) + 12) % 12 + 1);
  }

  // Where the week starts for this visitor. Not every browser answers, and
  // the fallback is Monday rather than Sunday because that is what the
  // people this page is handed to expect.
  var WEEK_START = (function () {
    try {
      var loc = new Intl.Locale(navigator.language);
      var info = loc.weekInfo || (loc.getWeekInfo && loc.getWeekInfo());
      if (info && info.firstDay) { return info.firstDay % 7; }  // 7 (Sun) → 0
    } catch (e) { /* fall through */ }
    return 1;
  })();

  var days = [];       // [{key, slots:[iso]}] ascending, only days with times
  var byDay = {};
  var chosenKey = "";  // the day whose times are listed
  var shownMonth = ""; // "YYYY-MM" the grid is showing

  function render(slots) {
    slotsBox.textContent = "";
    if (!slots.length) {
      var none = document.createElement("div");
      none.className = "text-muted small";
      none.textContent = "No free times in the next weeks. Try a shorter appointment.";
      slotsBox.appendChild(none);
      return;
    }
    // A month at a time, days first and times second: the question a visitor
    // opens this page with is "which day can I even have", and a fortnight of
    // half-hours in one list is a wall that doesn't answer it.
    days = [];
    byDay = {};
    slots.forEach(function (iso) {
      var key = dayKey(iso);
      if (!byDay[key]) { byDay[key] = { key: key, slots: [] }; days.push(byDay[key]); }
      byDay[key].slots.push(iso);
    });
    days.sort(function (a, b) { return a.key < b.key ? -1 : 1; });
    if (!byDay[chosenKey]) { chosenKey = days[0].key; }
    var first = monthOf(days[0].key);
    var last = monthOf(days[days.length - 1].key);
    if (shownMonth < first || shownMonth > last) { shownMonth = monthOf(chosenKey); }

    var pick = document.createElement("div");
    pick.className = "book-pick";
    pick.appendChild(calendar(slots, first, last));
    pick.appendChild(times());
    slotsBox.appendChild(pick);
  }

  function calendar(slots, first, last) {
    var box = document.createElement("div");
    box.className = "book-cal";

    var head = document.createElement("div");
    head.className = "book-cal-head";
    head.appendChild(arrow("‹", shownMonth > first, function () {
      goToMonth(monthShift(shownMonth, -1), slots);
    }));
    var title = document.createElement("div");
    title.className = "book-cal-title";
    title.textContent = new Intl.DateTimeFormat(undefined,
      { month: "long", year: "numeric" }).format(civil(shownMonth + "-01"));
    head.appendChild(title);
    head.appendChild(arrow("›", shownMonth < last, function () {
      goToMonth(monthShift(shownMonth, 1), slots);
    }));
    box.appendChild(head);

    var grid = document.createElement("div");
    grid.className = "book-cal-grid";
    for (var w = 0; w < 7; w++) {
      var wd = document.createElement("div");
      wd.className = "book-cal-wd";
      // 2024-01-07 was a Sunday, so day-of-week i is that date plus i.
      wd.textContent = new Intl.DateTimeFormat(undefined, {
        weekday: "short", timeZone: "UTC"
      }).format(new Date(Date.UTC(2024, 0, 7 + (WEEK_START + w) % 7)));
      grid.appendChild(wd);
    }

    var y = +shownMonth.slice(0, 4);
    var m = +shownMonth.slice(5, 7) - 1;
    var lead = (new Date(y, m, 1).getDay() - WEEK_START + 7) % 7;
    for (var b = 0; b < lead; b++) {
      grid.appendChild(document.createElement("div"));
    }
    var last_day = new Date(y, m + 1, 0).getDate();
    var today = dayKey(new Date().toISOString());
    for (var d = 1; d <= last_day; d++) {
      var key = shownMonth + "-" + pad(d);
      grid.appendChild(cell(key, d, key === today, slots));
    }
    box.appendChild(grid);

    var count = document.createElement("div");
    count.className = "book-tz mt-2";
    count.textContent = days.length === 1 ? "1 day with free times"
      : days.length + " days with free times";
    box.appendChild(count);
    return box;
  }

  function cell(key, number, isToday, slots) {
    var wrap = document.createElement("div");
    wrap.className = "book-cal-cell";
    var day = byDay[key];
    if (!day) {
      // Not a button and not styled like one: "nothing free here" has to be
      // readable without trying it.
      var flat = document.createElement("div");
      flat.className = "book-cal-none" + (isToday ? " book-cal-today" : "");
      flat.textContent = String(number);
      wrap.appendChild(flat);
      return wrap;
    }
    var b = document.createElement("button");
    b.type = "button";
    b.className = "btn btn-sm book-cal-day " +
      (key === chosenKey ? "btn-primary" : "btn-outline-secondary") +
      (isToday ? " book-cal-today" : "");
    b.innerHTML = "<span>" + number + "</span><span class='book-cal-dot'></span>";
    b.setAttribute("aria-label", day.slots.length + " free times");
    b.addEventListener("click", function () {
      chosenKey = key;
      render(slots);
    });
    wrap.appendChild(b);
    return wrap;
  }

  function goToMonth(ym, slots) {
    shownMonth = ym;
    // Land on something bookable: an arrow that moves the grid but leaves the
    // times beside it on a day three weeks back reads as a broken page.
    var inMonth = days.filter(function (d) { return monthOf(d.key) === ym; });
    if (inMonth.length) { chosenKey = inMonth[0].key; }
    render(slots);
  }

  function civil(key) {
    // A calendar date, not an instant: used only for month and weekday names.
    return new Date(+key.slice(0, 4), +key.slice(5, 7) - 1, +key.slice(8, 10));
  }

  function times() {
    var box = document.createElement("div");
    box.className = "book-times";
    var head = document.createElement("div");
    head.className = "book-day-name";
    head.textContent = new Intl.DateTimeFormat(undefined, {
      weekday: "long", day: "numeric", month: "long"
    }).format(civil(chosenKey));
    box.appendChild(head);

    var row = document.createElement("div");
    row.className = "book-slots";
    byDay[chosenKey].slots.forEach(function (iso) {
      var b = document.createElement("button");
      b.type = "button";
      b.className = "btn btn-sm btn-outline-secondary book-slot";
      b.textContent = fmt(iso, { hour: "2-digit", minute: "2-digit" });
      b.addEventListener("click", function () { choose(iso); });
      row.appendChild(b);
    });
    box.appendChild(row);
    return box;
  }

  function arrow(glyph, enabled, onClick) {
    var b = document.createElement("button");
    b.type = "button";
    b.className = "btn btn-sm btn-outline-secondary book-cal-arrow";
    b.textContent = glyph;
    b.disabled = !enabled;
    b.setAttribute("aria-label", glyph === "‹" ? "Previous month" : "Next month");
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
    chosenKey = "";
    shownMonth = "";
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
