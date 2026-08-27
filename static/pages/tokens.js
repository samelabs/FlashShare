// ── API tokens (agent/MCP channel) ──
const _tokenList = document.getElementById('tokenList');
const _newTokenBox = document.getElementById('newTokenBox');
const _newTokenValue = document.getElementById('newTokenValue');

async function loadTokens() {
  const r = await fetch('/api/tokens');
  const d = await r.json();
  if (!d.ok) return;
  _tokenList.innerHTML = '';
  for (const t of d.tokens.filter(x => x.status === 'active')) {
    const row = document.createElement('div');
    row.className = 'token-item';
    const status = 'token-active';
    const label = document.createElement('span');
    label.className = 'token-name';
    label.textContent = `${t.name} · fsk_${t.prefix}…`;
    const badge = document.createElement('span');
    badge.className = status;
    badge.textContent = window.t("active");
    row.append(label, badge);
    if (t.status === 'active') {
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'btn-secondary btn-token-revoke';
      const span = document.createElement('span');
      span.setAttribute('data-i18n', 'revokeToken');
      span.textContent = window.t("revokeToken");
      btn.appendChild(span);
      btn.addEventListener('click', async () => {
        if (!confirm(window.t("confirmRevokeToken"))) return;
        const rr = await fetch(`/api/tokens/${t.id}/revoke`, { method: 'POST' });
        if (rr.ok) loadTokens();
        else { const dd = await rr.json(); alert(dd.error || 'error'); }
      });
      row.appendChild(btn);
    }
    _tokenList.appendChild(row);
  }
}

document.getElementById('createToken')?.addEventListener('click', async () => {
  const name = document.getElementById('newTokenName').value.trim();
  const r = await fetch('/api/tokens', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name })
  });
  const d = await r.json();
  if (!d.ok) { alert(d.error || 'error'); return; }
  document.getElementById('newTokenNameEcho').textContent = name || '';
  _newTokenValue.textContent = d.token.token;
  _newTokenBox.hidden = false;
  document.getElementById('newTokenName').value = '';
  loadTokens();
});

const _copyBtn = document.getElementById('copyNewToken');
_copyBtn?.addEventListener('click', async () => {
  try { await navigator.clipboard.writeText(_newTokenValue.textContent); } catch (e) {}
  const span = _copyBtn.querySelector('[data-i18n]');
  if (span) { span.dataset.i18n = 'copied'; span.textContent = window.t("copied"); }
  setTimeout(() => { if (span) { span.dataset.i18n = 'copy'; span.textContent = window.t("copy"); } }, 2000);
});

if (_tokenList) loadTokens();
