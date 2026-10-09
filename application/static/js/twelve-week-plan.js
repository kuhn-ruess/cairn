/* 12 Week Year plan form: add and remove tactic rows, all/none weeks, and
 * goal names that follow what is typed. No request is made here; the form
 * posts as a whole and the server renders it back with every value kept.
 * Delegated on document because the app's <main> refresh replaces the DOM. */
(function () {
  "use strict";

  function form() { return document.getElementById("twy-plan"); }

  function addRow() {
    const f = form();
    const tpl = document.getElementById("twy-row-template");
    const box = document.getElementById("twy-rows");
    if (!f || !tpl || !box) return;
    const idx = parseInt(f.dataset.nextIdx || "0", 10);
    f.dataset.nextIdx = String(idx + 1);
    const holder = document.createElement("div");
    holder.innerHTML = tpl.innerHTML.split("__IDX__").join(String(idx));
    const row = holder.firstElementChild;
    syncGoalNames(row);
    box.appendChild(row);
    const title = row.querySelector('input[type="text"]');
    if (title) title.focus();
  }

  // The goal chips name the goals as typed above, before anything is saved;
  // a slot without a name shows no chip unless one is already picked.
  function syncGoalNames(scope) {
    document.querySelectorAll("[data-goal-title]").forEach(function (input) {
      const slot = input.dataset.goalTitle;
      const name = input.value.trim();
      (scope || document).querySelectorAll('label[data-goal-slot="' + slot + '"]').forEach(function (chip) {
        chip.textContent = name || ("Goal " + (parseInt(slot, 10) + 1));
        const box = document.getElementById(chip.htmlFor);
        chip.hidden = !name && !(box && box.checked);
      });
    });
  }

  function syncNoGoal(row) {
    const hint = row.querySelector("[data-twy-nogoal]");
    const any = row.querySelector('input[name$="-goal"]:checked');
    if (hint) hint.hidden = !!any;
  }

  document.addEventListener("click", function (ev) {
    if (!form()) return;
    const add = ev.target.closest("[data-twy-add]");
    if (add) { ev.preventDefault(); addRow(); return; }
    const row = ev.target.closest("[data-twy-row]");
    if (!row) return;
    if (ev.target.closest("[data-twy-remove]")) {
      ev.preventDefault();
      row.remove();
    } else if (ev.target.closest("[data-twy-all]") || ev.target.closest("[data-twy-none]")) {
      ev.preventDefault();
      const on = !!ev.target.closest("[data-twy-all]");
      row.querySelectorAll('.twy-weeks input[type="checkbox"]').forEach(function (b) { b.checked = on; });
    }
  });

  document.addEventListener("input", function (ev) {
    if (ev.target.matches && ev.target.matches("[data-goal-title]")) syncGoalNames();
  });

  // "Create it in Habitify" only makes sense with the dates managed.
  document.addEventListener("change", function (ev) {
    if (ev.target.matches && ev.target.matches('input[name$="-goal"]')) {
      const r = ev.target.closest("[data-twy-row]");
      if (r) syncNoGoal(r);
      return;
    }
    const sel = ev.target.closest && ev.target.closest("[data-twy-habit]");
    if (!sel || sel.value !== "__new__") return;
    const row = sel.closest("[data-twy-row]");
    const box = row && row.querySelector("[data-twy-manage]");
    if (box) box.checked = true;
  });
})();
