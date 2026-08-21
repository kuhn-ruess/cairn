// Follow a team's Zammad sync while it runs in a worker.
//
// The run takes minutes — longer than the gateway waits — so the button hands
// it to Celery and this polls what the worker publishes. Lives in its own file
// rather than inline on the page: a bktodo:refresh-needed swap replaces
// <main>.innerHTML and would drop any script inside it.
(function () {
  var POLL_MS = 2000;
  var timer = null;

  function box() { return document.getElementById('zammad-sync-progress'); }

  function render(el, data) {
    var bar = el.querySelector('[data-role="bar"]');
    var label = el.querySelector('[data-role="label"]');
    var counts = el.querySelector('[data-role="counts"]');
    var total = data.total || 0;
    var done = data.done || 0;
    var pct = total ? Math.round((done / total) * 100) : 0;

    if (data.state === 'queued') {
      label.textContent = 'Waiting for a worker…';
    } else if (data.state === 'fetching') {
      label.textContent = 'Reading the ticket list…';
    } else if (data.state === 'finishing') {
      label.textContent = 'Finishing up…';
      pct = 100;
    } else {
      label.textContent = total
        ? 'Ticket ' + done + ' of ' + total
        : 'Syncing…';
    }
    // Nothing to measure yet → a full striped bar reads as "working", not "done".
    bar.style.width = (total ? pct : 100) + '%';
    bar.classList.toggle('bg-secondary', !total);

    var parts = [];
    if (data.created) parts.push(data.created + ' new');
    if (data.updated) parts.push(data.updated + ' updated');
    if (data.entries) parts.push(data.entries + ' time entries');
    if (data.errors) parts.push(data.errors + ' errors');
    counts.textContent = parts.join(' · ');
  }

  function stall(el, polls) {
    // A run nobody picked up looks exactly like a slow one — say so instead of
    // spinning forever.
    if (polls * POLL_MS < 60000) return;
    el.querySelector('[data-role="hint"]').innerHTML =
      '<span class="text-warning">No worker has picked this run up in the last '
      + 'minute. Check that the <code>worker</code> container is running.</span>';
  }

  function start() {
    var el = box();
    if (!el || timer) return;
    var polls = 0;
    el.classList.remove('d-none');
    timer = setInterval(function () {
      fetch(el.dataset.url, { headers: { 'X-Requested-With': 'fetch' } })
        .then(function (r) { return r.ok ? r.json() : null; })
        .then(function (data) {
          if (!data) return;
          polls += 1;
          if (data.running) {
            render(el, data);
            if (data.state === 'queued') stall(el, polls);
            return;
          }
          clearInterval(timer);
          timer = null;
          // Done or failed: reload so the result card, the sync log and the
          // member table all show what the run actually did.
          window.location.reload();
        })
        .catch(function () { /* a blip; the next tick tries again */ });
    }, POLL_MS);
  }

  document.addEventListener('DOMContentLoaded', function () {
    var el = box();
    if (el && el.dataset.running) start();
  });
})();
