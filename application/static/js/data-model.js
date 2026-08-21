// The data-model page: keep #m-<Model> links working across tabs.
//
// The models are split into tabs, so a link to one in a tab that isn't open
// used to land on nothing — including every #m-… link written down before the
// tabs existed. Open the tab the target lives in, then scroll to it.
(function () {
  function reveal(hash) {
    if (!hash || hash.indexOf('#m-') !== 0) return;
    var target = document.getElementById(hash.slice(1));
    if (!target) return;
    var pane = target.closest('.tab-pane');
    if (pane && !pane.classList.contains('active')) {
      var btn = document.querySelector('[data-bs-target="#' + pane.id + '"]');
      if (btn && window.bootstrap) bootstrap.Tab.getOrCreateInstance(btn).show();
    }
    target.scrollIntoView({ block: 'start' });
  }

  document.addEventListener('DOMContentLoaded', function () {
    if (!document.querySelector('[data-role="data-model"]')) return;
    reveal(window.location.hash);
  });
  window.addEventListener('hashchange', function () {
    if (!document.querySelector('[data-role="data-model"]')) return;
    reveal(window.location.hash);
  });
})();
