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

    const showBtn = document.createElement('button');
    showBtn.type = 'button';
    showBtn.className = 'btn btn-secondary btn-key-show';
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
    copyBtn.className = 'btn btn-secondary btn-key-copy hidden';
    const copySpan = document.createElement('span');
    copySpan.setAttribute('data-i18n', 'copy');
    copySpan.textContent = window.t('copy');
    copyBtn.appendChild(copySpan);
    copyBtn.addEventListener('click', () => {
      _copyRaw(copyBtn, value.textContent);
    });
    actions.appendChild(copyBtn);

    const delBtn = document.createElement('button');
    delBtn.type = 'button';
    delBtn.className = 'btn btn-secondary btn-key-delete';
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

// ── Authorized applications (OAuth grants) — list / revoke ──
// Rows render the client hostname only; the raw client_id travels in
// the revoke request and never reaches the visible surface.
const _grantList = document.getElementById('grantList');

const _GRANT_SCOPE_KEYS = {
  'tools:call': 'oauth_scope_tools_call',
  'files:read': 'oauth_scope_files_read',
  'files:delete': 'oauth_scope_files_delete',
};

function _grantTime(ts) {
  if (!ts) return '';
  try { return new Date(ts * 1000).toLocaleString(); } catch (e) { return ''; }
}

function _grantMeta(g) {
  // one full-width line: scope descriptions + last use; pure-copy
  // fragments are data-i18n spans, dynamic values are plain text nodes
  const meta = document.createElement('div');
  meta.className = 'key-value';
  let first = true;
  for (const s of String(g.scope || '').split(/\s+/)) {
    const key = _GRANT_SCOPE_KEYS[s];
    if (!key) continue;
    if (!first) meta.appendChild(document.createTextNode(' · '));
    first = false;
    const item = document.createElement('span');
    item.setAttribute('data-i18n', key);
    item.textContent = window.t(key);
    meta.appendChild(item);
  }
  const used = _grantTime(g.last_used_at);
  if (used) {
    if (!first) meta.appendChild(document.createTextNode(' · '));
    const label = document.createElement('span');
    label.setAttribute('data-i18n', 'oauth_last_used');
    label.textContent = window.t('oauth_last_used');
    meta.appendChild(label);
    meta.appendChild(document.createTextNode(' ' + used));
  }
  return meta;
}

async function loadGrants() {
  if (!_grantList) return;
  const r = await fetch('/api/oauth/grants');
  const d = await r.json();
  if (!d.ok) return;
  _grantList.innerHTML = '';
  if (!d.grants.length) {
    const empty = document.createElement('p');
    empty.className = 'profile-note';
    const span = document.createElement('span');
    span.setAttribute('data-i18n', 'oauth_grants_empty');
    span.textContent = window.t('oauth_grants_empty');
    empty.appendChild(span);
    _grantList.appendChild(empty);
    return;
  }
  for (const g of d.grants) {
    const row = document.createElement('div');
    row.className = 'key-item';

    const label = document.createElement('span');
    label.className = 'key-name';
    label.textContent = g.client_host;

    const actions = document.createElement('span');
    actions.className = 'key-actions';

    const revokeBtn = document.createElement('button');
    revokeBtn.type = 'button';
    revokeBtn.className = 'btn btn-secondary btn-grant-revoke';
    const revokeSpan = document.createElement('span');
    revokeSpan.setAttribute('data-i18n', 'oauth_revoke_btn');
    revokeSpan.textContent = window.t('oauth_revoke_btn');
    revokeBtn.appendChild(revokeSpan);
    revokeBtn.addEventListener('click', async () => {
      // Site-wide confirm authority — no native confirm() (VIS-006).
      const ok = await window.confirmAction({
        title: window.t('oauth_revoke_btn'),
        message: window.t('confirmGrantRevoke'),
        confirmLabel: window.t('oauth_revoke_btn'),
        cancelLabel: window.t('cancel'),
      });
      if (!ok) return;
      const rr = await fetch('/api/oauth/grants/revoke', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ client_id: g.client_id })
      });
      if (rr.ok) loadGrants();
      else { const dd = await rr.json(); alert(_errm(dd.error)); }
    });
    actions.appendChild(revokeBtn);

    row.append(label, actions, _grantMeta(g));
    _grantList.appendChild(row);
  }
}

if (_grantList) loadGrants();
