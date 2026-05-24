/* BandSox Shared Utilities */

// ── Auth intercept ────────────────────────────────────────────────
const _originalFetch = window.fetch;
window.fetch = async function (...args) {
  const response = await _originalFetch(...args);
  if (response.status === 401) window.location.href = '/login';
  return response;
};

async function logout() {
  await _originalFetch('/api/auth/logout', { method: 'POST' });
  localStorage.removeItem('bandsox_session_token');
  window.location.href = '/login';
}

// ── Utilities ─────────────────────────────────────────────────────
function escapeHtml(text) {
  const div = document.createElement('div');
  div.textContent = text;
  return div.innerHTML;
}

// ── Modal helpers ─────────────────────────────────────────────────
function openModal(id) { document.getElementById(id).classList.add('open'); }
function closeModalById(id) { document.getElementById(id).classList.remove('open'); }

let _confirmCallback = null;

function showConfirmModal(title, message, onConfirm, detailsHtml = '') {
  _confirmCallback = onConfirm;
  document.getElementById('confirm-title').textContent = title;
  document.getElementById('confirm-message').textContent = message;

  const details = document.getElementById('confirm-details');
  if (details) {
    if (detailsHtml && detailsHtml.trim()) {
      details.innerHTML = detailsHtml;
      details.style.display = 'block';
    } else {
      details.innerHTML = '';
      details.style.display = 'none';
    }
  }
  openModal('confirm-modal');
}

function closeConfirmModal() {
  closeModalById('confirm-modal');
  const details = document.getElementById('confirm-details');
  if (details) {
    details.innerHTML = '';
    details.style.display = 'none';
  }
  _confirmCallback = null;
}

async function confirmAction() {
  if (_confirmCallback) {
    const cb = _confirmCallback;
    closeConfirmModal();
    try { await cb(); } catch (e) { console.error(e); }
  }
}

// ── Loading spinner ───────────────────────────────────────────────
const _spinnerSvg = '<svg class="spin" width="14" height="14" xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24"><circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"/><path fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"/></svg>';

function setLoading(btnId, isLoading) {
  const btn = document.getElementById(btnId);
  if (!btn) return;
  if (isLoading) {
    if (!btn.dataset.originalHtml) btn.dataset.originalHtml = btn.innerHTML;
    btn.disabled = true; btn.style.opacity = '0.5';
    btn.innerHTML = _spinnerSvg;
  } else {
    btn.disabled = false; btn.style.opacity = '';
    if (btn.dataset.originalHtml) btn.innerHTML = btn.dataset.originalHtml;
  }
}

// ── Selection manager ─────────────────────────────────────────────
// Creates a reusable selection controller for checkbox-based lists.
//
// config: {
//   masterCheckboxId:  ID of select-all checkbox
//   checkboxClass:     class on individual row checkboxes
//   bulkBtnId:         ID of the bulk-action button
//   countSpanId:       ID of the span showing selected count
// }
function createSelectionManager(config) {
  const selected = new Set();

  function getCheckboxes() {
    return document.querySelectorAll('.' + config.checkboxClass);
  }

  function updateMaster() {
    const master = document.getElementById(config.masterCheckboxId);
    const boxes = getCheckboxes();
    if (!master || !boxes.length) return;
    const allChecked = Array.from(boxes).every(c => c.checked);
    const someChecked = Array.from(boxes).some(c => c.checked);
    master.checked = allChecked;
    master.indeterminate = !allChecked && someChecked;
  }

  function updateBulkUI() {
    const btn = document.getElementById(config.bulkBtnId);
    if (!btn) return;
    const span = document.getElementById(config.countSpanId);
    if (span) span.textContent = selected.size;
    btn.style.display = selected.size > 0 ? 'inline-flex' : 'none';
  }

  return {
    selected,

    toggleAll() {
      const master = document.getElementById(config.masterCheckboxId);
      getCheckboxes().forEach(cb => {
        cb.checked = master.checked;
        if (master.checked) selected.add(cb.value);
        else selected.delete(cb.value);
      });
      updateBulkUI();
    },

    toggle(checkbox) {
      if (checkbox.checked) selected.add(checkbox.value);
      else selected.delete(checkbox.value);
      updateMaster();
      updateBulkUI();
    },

    syncWithData(currentIds) {
      for (const id of selected) {
        if (!currentIds.has(id)) selected.delete(id);
      }
      updateBulkUI();
    },

    clear() {
      selected.clear();
      const master = document.getElementById(config.masterCheckboxId);
      if (master) { master.checked = false; master.indeterminate = false; }
      updateBulkUI();
    },

    updateMaster,
    updateBulkUI,
  };
}

// ── Metadata editor ───────────────────────────────────────────────
// Creates a reusable key-value metadata editor.
//
// config: {
//   containerId:  ID of the entries container div
//   keyInputId:   ID of the new-key input
//   valueInputId: ID of the new-value input
//   errorId:      ID of the error message element
// }
function createMetadataEditor(config) {
  let data = {};

  const trashIcon = '<svg width="13" height="13" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16"/></svg>';

  function render() {
    const container = document.getElementById(config.containerId);
    const entries = Object.entries(data);
    if (!entries.length) {
      container.innerHTML = '<p style="color:var(--text-ghost);font-size:12px;text-align:center;padding:16px 0;">No metadata. Add entries above.</p>';
      return;
    }
    container.innerHTML = entries.map(([k, v]) => `
      <div class="metadata-entry">
        <input class="meta-key" type="text" value="${escapeHtml(k)}" onchange="${config.ns}.updateKey('${escapeHtml(k)}',this.value)">
        <span class="meta-sep">:</span>
        <input class="meta-val" type="text" value="${escapeHtml(String(v))}" onchange="${config.ns}.updateValue('${escapeHtml(k)}',this.value)">
        <button class="meta-del" onclick="${config.ns}.remove('${escapeHtml(k)}')">${trashIcon}</button>
      </div>
    `).join('');
  }

  const editor = {
    getData() { return data; },
    setData(d) { data = { ...d }; render(); },

    add() {
      const keyEl = document.getElementById(config.keyInputId);
      const valEl = document.getElementById(config.valueInputId);
      const errEl = document.getElementById(config.errorId);
      const key = keyEl.value.trim();
      if (!key) { errEl.textContent = 'Key is required'; errEl.style.display = 'block'; return; }
      if (key in data) { errEl.textContent = 'Key already exists'; errEl.style.display = 'block'; return; }
      data[key] = valEl.value.trim();
      keyEl.value = ''; valEl.value = '';
      errEl.style.display = 'none';
      render();
      keyEl.focus();
    },

    updateKey(oldKey, newKey) {
      if (!newKey.trim() || (newKey !== oldKey && newKey in data)) { render(); return; }
      const val = data[oldKey];
      delete data[oldKey];
      data[newKey] = val;
    },

    updateValue(key, val) { data[key] = val; },

    remove(key) { delete data[key]; render(); },

    render,
  };

  return editor;
}

// ── DOMContentLoaded: wire confirm modal ──────────────────────────
document.addEventListener('DOMContentLoaded', () => {
  const confirmEl = document.getElementById('confirm-modal');
  if (confirmEl) {
    confirmEl.addEventListener('click', e => { if (e.target.id === 'confirm-modal') closeConfirmModal(); });
  }
});
