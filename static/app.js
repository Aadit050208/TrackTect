/* TrackTect shell: theme, command palette, alerts, bulk select. */

function toggleMode() {
  document.body.classList.toggle('light');
  localStorage.setItem('tracktect-theme',
    document.body.classList.contains('light') ? 'light' : 'dark');
}

function goBack() {
  try {
    if (window.history.length > 1) {
      window.history.back();
      return;
    }
  } catch (e) {}
  window.location.href = window.__dashboardUrl || '/dashboard';
}

function toggleAlerts(btn) {
  var dropdown = document.getElementById('alert-dropdown');
  if (!dropdown) return;
  var open = dropdown.hasAttribute('hidden');
  if (open) {
    dropdown.removeAttribute('hidden');
    btn.setAttribute('aria-expanded', 'true');
  } else {
    dropdown.setAttribute('hidden', '');
    btn.setAttribute('aria-expanded', 'false');
  }
}

document.addEventListener('click', function (event) {
  var center = document.querySelector('.alert-center');
  var dropdown = document.getElementById('alert-dropdown');
  if (!center || !dropdown || dropdown.hasAttribute('hidden')) return;
  if (!center.contains(event.target)) {
    dropdown.setAttribute('hidden', '');
    var bell = center.querySelector('.alert-bell');
    if (bell) bell.setAttribute('aria-expanded', 'false');
  }
});

/* ---------------- bulk select ---------------- */

(function () {
  var checks = document.querySelectorAll('.bulk-check');
  var bar = document.getElementById('bulk-bar');
  if (!checks.length || !bar) return;

  function sync() {
    var n = document.querySelectorAll('.bulk-check:checked').length;
    bar.hidden = n === 0;
  }
  checks.forEach(function (c) { c.addEventListener('change', sync); });
})();

/* ---------------- sidebar competitor filter ---------------- */

(function () {
  var input = document.getElementById('comp-filter');
  if (!input) return;
  var rows = document.querySelectorAll('.side-item-row[data-comp-name]');
  var empty = document.getElementById('comp-filter-empty');

  function apply() {
    var q = input.value.trim().toLowerCase();
    var visible = 0;
    rows.forEach(function (row) {
      var hay = row.getAttribute('data-comp-name') || '';
      var show = !q || hay.indexOf(q) !== -1;
      row.classList.toggle('is-hidden', !show);
      if (show) visible += 1;
    });
    if (empty) empty.hidden = !(q && visible === 0);
  }

  input.addEventListener('input', apply);
})();

/* ---------------- command palette ---------------- */

var paletteOverlay = document.getElementById('palette-overlay');
var paletteInput = document.getElementById('palette-input');
var paletteList = document.getElementById('palette-list');
var paletteSelection = 0;
var paletteMatches = [];

function openPalette() {
  if (!paletteOverlay) return;
  paletteOverlay.classList.add('open');
  paletteInput.value = '';
  renderPalette('');
  paletteInput.focus();
}

function closePalette() {
  if (!paletteOverlay) return;
  paletteOverlay.classList.remove('open');
}

function renderPalette(query) {
  var q = query.trim().toLowerCase();
  paletteMatches = (window.__paletteItems || []).filter(function (item) {
    return !q || item.label.toLowerCase().indexOf(q) !== -1;
  });

  // Offer a full-text search jump when the query looks like content search.
  if (q && window.__searchUrl) {
    paletteMatches.push({
      label: 'Search “' + query.trim() + '” (list & changes)',
      kind: 'search',
      href: window.__searchUrl + '?q=' + encodeURIComponent(query.trim()),
    });
    if (window.__addCompetitorUrl) {
      paletteMatches.push({
        label: 'Look up brand “' + query.trim() + '”',
        kind: 'action',
        href: window.__addCompetitorUrl + '?prefill=' + encodeURIComponent(query.trim()),
      });
    }
  }

  paletteSelection = 0;
  if (!paletteMatches.length) {
    paletteList.innerHTML = '<div class="palette-empty">No matches</div>';
    return;
  }
  paletteList.innerHTML = paletteMatches.map(function (item, i) {
    return '<div class="palette-item' + (i === 0 ? ' selected' : '') + '" data-i="' + i + '" role="option" aria-selected="' + (i === 0) + '">'
      + '<span>' + escapeHtml(item.label) + '</span>'
      + '<span class="kind">' + item.kind + '</span></div>';
  }).join('');
}

function escapeHtml(text) {
  var div = document.createElement('div');
  div.textContent = text;
  return div.innerHTML;
}

function updateSelection() {
  paletteList.querySelectorAll('.palette-item').forEach(function (el, i) {
    el.classList.toggle('selected', i === paletteSelection);
    el.setAttribute('aria-selected', i === paletteSelection ? 'true' : 'false');
    if (i === paletteSelection) el.scrollIntoView({ block: 'nearest' });
  });
}

if (paletteOverlay) {
  paletteInput.addEventListener('input', function () { renderPalette(paletteInput.value); });

  paletteInput.addEventListener('keydown', function (event) {
    if (event.key === 'ArrowDown') {
      event.preventDefault();
      paletteSelection = Math.min(paletteSelection + 1, paletteMatches.length - 1);
      updateSelection();
    } else if (event.key === 'ArrowUp') {
      event.preventDefault();
      paletteSelection = Math.max(paletteSelection - 1, 0);
      updateSelection();
    } else if (event.key === 'Enter' && paletteMatches[paletteSelection]) {
      window.location.href = paletteMatches[paletteSelection].href;
    } else if (event.key === 'Escape') {
      closePalette();
    }
  });

  paletteList.addEventListener('click', function (event) {
    var item = event.target.closest('.palette-item');
    if (item) window.location.href = paletteMatches[Number(item.dataset.i)].href;
  });

  paletteOverlay.addEventListener('mousedown', function (event) {
    if (event.target === paletteOverlay) closePalette();
  });

  document.addEventListener('keydown', function (event) {
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'k') {
      event.preventDefault();
      paletteOverlay.classList.contains('open') ? closePalette() : openPalette();
    } else if (event.key === 'Escape' && paletteOverlay.classList.contains('open')) {
      closePalette();
    }
  });
}

/* ---------------- busy / loading feedback ---------------- */

(function () {
  function ensureOverlay() {
    var el = document.getElementById('busy-overlay');
    if (el) return el;
    el = document.createElement('div');
    el.id = 'busy-overlay';
    el.className = 'busy-overlay';
    el.setAttribute('hidden', '');
    el.innerHTML =
      '<div class="busy-card" role="status" aria-live="polite">' +
      '  <div class="busy-spinner" aria-hidden="true"></div>' +
      '  <div class="busy-title" id="busy-title">Working…</div>' +
      '  <div class="busy-detail" id="busy-detail">Please wait — results are being prepared.</div>' +
      '</div>';
    document.body.appendChild(el);
    return el;
  }

  function showBusy(title, detail) {
    var el = ensureOverlay();
    var t = document.getElementById('busy-title');
    var d = document.getElementById('busy-detail');
    if (t) t.textContent = title || 'Working…';
    if (d) d.textContent = detail || 'Please wait — results are being prepared on the server.';
    el.removeAttribute('hidden');
    document.body.classList.add('is-busy');
  }

  window.__showBusy = showBusy;

  document.addEventListener('submit', function (event) {
    var form = event.target;
    if (!form || form.tagName !== 'FORM') return;
    if (!form.hasAttribute('data-busy') && !form.querySelector('[data-busy-label]')) return;

    var btn = form.querySelector('button[type="submit"], button:not([type])');
    if (event.submitter && event.submitter.tagName === 'BUTTON') btn = event.submitter;

    var label = (btn && btn.getAttribute('data-busy-label')) || 'Working…';
    var title = form.getAttribute('data-busy-title') || label;
    var detail = form.getAttribute('data-busy-detail') ||
      'Hang tight — this is running on the server and can take a few seconds.';

    // Visual feedback immediately (do NOT disable the submit button before
    // the browser queues the POST — that can cancel the request).
    if (btn) {
      btn.classList.add('is-loading');
      if (!btn.dataset.originalLabel) btn.dataset.originalLabel = btn.innerHTML;
      btn.innerHTML = '<span class="btn-spinner" aria-hidden="true"></span> ' + label;
    }
    showBusy(title, detail);

    // Soft-lock other controls after the submit has started.
    window.setTimeout(function () {
      form.querySelectorAll('button, input, select, textarea').forEach(function (el) {
        if (el === btn) return;
        el.setAttribute('readonly', 'readonly');
        if (el.tagName === 'BUTTON' || el.tagName === 'SELECT') el.disabled = true;
      });
      if (btn) btn.disabled = true;
    }, 40);
  }, true);
})();

/* ---------------- draft ticket buttons ---------------- */

document.addEventListener('click', function (event) {
  var btn = event.target.closest('.ticket-btn');
  if (!btn) return;
  var id = btn.getAttribute('data-insight-id');
  if (!id) return;
  window.location.href = '/insights/' + encodeURIComponent(id) + '/ticket';
});

/* Confirm before quota-consuming actions */
document.addEventListener('click', function (event) {
  var el = event.target.closest('[data-cost]');
  if (!el) return;
  if (el.disabled || el.getAttribute('aria-disabled') === 'true') return;
  var cost = el.getAttribute('data-cost');
  if (!cost || cost === '0') return;
  var label = el.getAttribute('data-cost-label')
    || ('This uses ' + cost + ' search' + (cost === '1' ? '' : 'es') + '. Continue?');
  if (!window.confirm(label)) {
    event.preventDefault();
    event.stopPropagation();
  }
}, true);
