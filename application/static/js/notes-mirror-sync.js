// The two "Sync now" buttons for the notes mirror: settings, and the notes
// navigator.
//
// A round is handed to a worker, so the only thing the POST can honestly
// answer is "started". Saying anything else — the time of the LAST round, say
// — reads as a result and is wrong twice over: it is the value that was there
// before the click, and the real one appears only when the page is reloaded
// by hand. So both buttons wait: they poll what the round itself leaves
// behind and speak when it has landed.
(function () {
  var POLL_MS = 3000;
  var GIVE_UP_MS = 15 * 60 * 1000;   // a first round writes hundreds of files

  // Wait for the round to land. ``before`` is the state as it was at the
  // click; a new stamp or a new status line is the round answering.
  window.cairnMirrorFollow = function (stateUrl, before, onDone, onGiveUp) {
    var started = Date.now();
    function tick() {
      if (Date.now() - started > GIVE_UP_MS) {
        if (onGiveUp) onGiveUp();
        return;
      }
      fetch(stateUrl, {
        credentials: 'same-origin',
        headers: { 'X-Requested-With': 'fetch' },
      })
        .then(function (r) { return r.ok ? r.json() : null; })
        .then(function (state) {
          // A blip is not an answer — keep waiting rather than reporting one.
          if (state && (state.at !== before.at
                        || state.status !== before.status)) {
            onDone(state);
            return;
          }
          setTimeout(tick, POLL_MS);
        })
        .catch(function () { setTimeout(tick, POLL_MS); });
    }
    setTimeout(tick, POLL_MS);
  };

  // --- the settings page ---
  document.addEventListener('DOMContentLoaded', function () {
    var form = document.querySelector('[data-mirror-sync]');
    if (!form) return;
    var button = form.querySelector('button[type="submit"]');
    var note = form.querySelector('[data-role="mirror-note"]');

    function say(text) { if (note) note.textContent = text; }

    form.addEventListener('submit', function (ev) {
      ev.preventDefault();
      button.disabled = true;
      say('Starting…');
      fetch(form.action, {
        method: 'POST',
        body: new FormData(form),
        credentials: 'same-origin',
        headers: { 'X-Requested-With': 'XMLHttpRequest' },
      })
        .then(function (r) { return r.json(); })
        .then(function (data) {
          if (!data.ok) {
            say(data.message);
            button.disabled = false;
            return;
          }
          if (!data.queued) {
            window.location.reload();     // it ran here: the page is stale
            return;
          }
          say('Syncing… this page updates when the round is done.');
          window.cairnMirrorFollow(form.dataset.stateUrl, data, function () {
            // Reload rather than patch: the round moved the last-round line,
            // the history table and the counters, and all three should agree.
            window.location.reload();
          }, function () {
            say('Still running. Reload this page to see how it ended.');
            button.disabled = false;
          });
        })
        .catch(function () {
          say('Could not start the sync.');
          button.disabled = false;
        });
    });
  });
})();
