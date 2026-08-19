/* Command bar — press "p" to open. Type to filter projects + tasks;
 * arrow keys + Enter to navigate. Ignores presses inside inputs so it
 * doesn't hijack normal typing.
 */
(function () {
  const modal = document.getElementById("quickJumpModal");
  if (!modal) return;
  const bsModal = bootstrap.Modal.getOrCreateInstance(modal);
  const input = modal.querySelector(".quickjump-input");
  const list = modal.querySelector(".quickjump-results");

  let items = [];
  let selectedIdx = 0;
  let fetchAbort = null;

  function escapeHtml(s) {
    return (s || "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;");
  }

  function iconFor(kind) {
    if (kind === "project") return "bi-folder";
    if (kind === "folder") return "bi-folder2-open";
    if (kind === "note") return "bi-journal-text";
    return "bi-check2-circle";
  }

  function render() {
    if (!items.length) {
      list.innerHTML = '<li class="text-muted small px-3 py-2">No matches</li>';
      return;
    }
    list.innerHTML = items
      .map(
        (it, i) =>
          `<li class="quickjump-item ${i === selectedIdx ? "active" : ""}"
                data-idx="${i}" data-url="${escapeHtml(it.url)}">
             <i class="bi ${iconFor(it.kind)}"></i>
             <span class="quickjump-title">${escapeHtml(it.title)}</span>
             ${it.subtitle ? `<span class="quickjump-sub">${escapeHtml(it.subtitle)}</span>` : ""}
           </li>`
      )
      .join("");
  }

  async function search(q) {
    if (fetchAbort) fetchAbort.abort();
    fetchAbort = new AbortController();
    if (!q) {
      items = [];
      selectedIdx = 0;
      render();
      return;
    }
    try {
      const resp = await fetch(
        "/tasks/quickjump?kinds=project,folder,task,note&q=" + encodeURIComponent(q),
        { signal: fetchAbort.signal }
      );
      if (!resp.ok) return;
      const data = await resp.json();
      items = data.results || [];
      selectedIdx = 0;
      render();
    } catch (err) {
      if (err.name === "AbortError") return;
    }
  }

  function navigateTo(idx) {
    const item = items[idx];
    if (!item) return;
    bsModal.hide();
    window.location.assign(item.url);
  }

  input.addEventListener("input", () => search(input.value.trim()));
  input.addEventListener("keydown", (e) => {
    if (e.key === "ArrowDown") {
      e.preventDefault();
      if (items.length) {
        selectedIdx = (selectedIdx + 1) % items.length;
        render();
      }
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      if (items.length) {
        selectedIdx = (selectedIdx - 1 + items.length) % items.length;
        render();
      }
    } else if (e.key === "Enter") {
      e.preventDefault();
      navigateTo(selectedIdx);
    }
  });

  list.addEventListener("click", (e) => {
    const li = e.target.closest(".quickjump-item");
    if (!li) return;
    navigateTo(parseInt(li.dataset.idx, 10));
  });

  modal.addEventListener("shown.bs.modal", () => {
    input.value = "";
    items = [];
    selectedIdx = 0;
    render();
    input.focus();
  });

  function shouldIgnore(e) {
    const t = e.target;
    if (!t) return false;
    const tag = (t.tagName || "").toUpperCase();
    if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") return true;
    if (t.isContentEditable) return true;
    if (e.ctrlKey || e.metaKey || e.altKey) return true;
    return false;
  }

  document.addEventListener("keydown", (e) => {
    if (e.key === "p" && !shouldIgnore(e)) {
      e.preventDefault();
      bsModal.show();
    }
  });

  // The navbar search box is a shortcut into this same command bar — focusing
  // or submitting it opens the quickjump overlay (carrying any typed text)
  // instead of navigating to the full search page.
  const navForm = document.getElementById("navbarSearchForm");
  const navInput = document.getElementById("navbarSearch");
  if (navForm && navInput) {
    const openWith = (seed) => {
      navInput.blur();
      bsModal.show();
      if (seed) {
        // 'shown.bs.modal' resets the input, so seed after it fires.
        modal.addEventListener("shown.bs.modal", function once() {
          modal.removeEventListener("shown.bs.modal", once);
          input.value = seed;
          search(seed);
        });
      }
    };
    navInput.addEventListener("focus", () => openWith(navInput.value.trim()));
    navForm.addEventListener("submit", (e) => {
      e.preventDefault();
      openWith(navInput.value.trim());
    });
  }
})();
