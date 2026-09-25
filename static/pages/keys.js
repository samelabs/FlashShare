// ── API keys (agent/MCP channel) — create / view / copy / delete ──
// The full key is viewable any time from the owner's browser session
// (the server keeps an encrypted copy); Bearer access cannot read it.
const _keyList = document.getElementById('keyList');

async function _copyRaw(btn, text) {
  // feedback applied synchronously; the clipboard write may await or
  // fail (headless / denied permission) and must not delay it
  const old = btn.innerHTML;
  btn.textContent = '✓ ' + window.t('copied');
  btn.classList.add('copied');
  try { await navigator.clipboard.writeText(text); } catch (e) {}
  setTimeout(() => { btn.innerHTML = old; btn.classList.remove('copied'); }, 1500);
}

async function loadKeys() {
  const r = await fetch('/api/keys');
  const d = await r.json();
  if (!d.ok) return;
  _keyList.innerHTML = '';
  for (const k of d.keys) {
    const row = document.createElement('div');
    row.className = 'key-item';
    row.dataset.id = k.id;

    const label = document.createElement('span');
    label.className = 'key-name';
    label.textContent = `${k.name} · fsk_${k.prefix}…`;

    const value = document.createElement('code');
    value.className = 'key-value';
    value.hidden = true;

    const actions = document.createElement('span');
    actions.className = 'key-actions';

    if (k.viewable) {
      const showBtn = document.createElement('button');
      showBtn.type = 'button';
      showBtn.className = 'btn-secondary btn-key-show';
      const showSpan = document.createElement('span');
      showSpan.setAttribute('data-i18n', 'keyShow');
      showSpan.textContent = window.t('keyShow');
      showBtn.appendChild(showSpan);
      showBtn.addEventListener('click', async () => {
        if (!value.hidden) { value.hidden = true; return; }
        const rr = await fetch(`/api/keys/${k.id}/reveal`);
        if (!rr.ok) { const dd = await rr.json(); alert(_errm(dd.error)); return; }
        const dd = await rr.json();
        value.textContent = dd.key;
        value.hidden = false;
        const copyBtn = actions.querySelector('.btn-key-copy');
        if (copyBtn) copyBtn.classList.remove('hidden');
      });
      actions.appendChild(showBtn);

      const copyBtn = document.createElement('button');
      copyBtn.type = 'button';
      copyBtn.className = 'btn-secondary btn-key-copy hidden';
      const copySpan = document.createElement('span');
      copySpan.setAttribute('data-i18n', 'copy');
      copySpan.textContent = window.t('copy');
      copyBtn.appendChild(copySpan);
      copyBtn.addEventListener('click', () => {
        _copyRaw(copyBtn, value.textContent);
      });
      actions.appendChild(copyBtn);
    } else {
      // historical row without a stored copy — prefix only, create a new
      // key to see the full value (data state, not an error)
      const note = document.createElement('span');
      note.className = 'key-legacy';
      note.setAttribute('data-i18n', 'keyLegacyHint');
      note.textContent = window.t('keyLegacyHint');
      actions.appendChild(note);
    }

    const delBtn = document.createElement('button');
    delBtn.type = 'button';
    delBtn.className = 'btn-secondary btn-key-delete';
    const delSpan = document.createElement('span');
    delSpan.setAttribute('data-i18n', 'keyDelete');
    delSpan.textContent = window.t('keyDelete');
    delBtn.appendChild(delSpan);
    delBtn.addEventListener('click', async () => {
      // Site-wide confirm authority — no native confirm() (VIS-006).
      const ok = await window.confirmAction({
        title: window.t('keyDelete'),
        message: window.t('confirmKeyDelete'),
        confirmLabel: window.t('keyDelete'),
        cancelLabel: window.t('cancel'),
      });
      if (!ok) return;
      const rr = await fetch(`/api/keys/${k.id}`, { method: 'DELETE' });
      if (rr.ok) loadKeys();
      else { const dd = await rr.json(); alert(_errm(dd.error)); }
    });
    actions.appendChild(delBtn);

    row.append(label, value, actions);
    _keyList.appendChild(row);
  }
}

document.getElementById('createKey')?.addEventListener('click', async () => {
  const name = document.getElementById('newKeyName').value.trim();
  const btn = document.getElementById('createKey');
  btn.disabled = true;
  try {
    const r = await fetch('/api/keys', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name })
    });
    const d = await r.json();
    if (!d.ok) { alert(_errm(d.error)); return; }
    document.getElementById('newKeyName').value = '';
    await loadKeys();
    // reveal the freshly created key immediately (it is viewable any
    // time — this is a convenience, not a one-time show)
    const first = _keyList.querySelector('.key-item .btn-key-show');
    if (first) first.click();
  } finally {
    btn.disabled = false;
  }
});

if (_keyList) loadKeys();
