const _wModal = document.getElementById('withdrawModal');

function _pager(el, base, pageKey, page, total, per) {
  const pages = Math.max(1, Math.ceil(total / per));
  el.classList.toggle('hidden', pages <= 1);
  if (pages <= 1) return;
  const q = p => { const u = new URL(window.location.href); u.searchParams.set(pageKey, p); return u.pathname + '?' + u.searchParams.toString(); };
  el.innerHTML = '';
  const prev = document.createElement(page > 1 ? 'a' : 'span');
  if (page > 1) prev.href = q(page - 1); prev.className = page > 1 ? '' : 'off';
  prev.textContent = '← ' + _msg('prev_page');
  const mid = document.createElement('span'); mid.className = 'grow'; mid.textContent = page + ' / ' + pages;
  const next = document.createElement(page < pages ? 'a' : 'span');
  if (page < pages) next.href = q(page + 1); next.className = page < pages ? '' : 'off';
  next.textContent = _msg('next_page') + ' →';
  el.append(prev, mid, next);
}

function _usd(cents) {
  return '$' + ((Number(cents) || 0) / 100).toFixed(2);
}

async function loadEarnings() {
  const r = await fetch('/api/earnings');
  if (!r.ok) return;
  const d = await r.json();
  const s = d.summary;
  document.getElementById('eTotal').textContent = s.earned_usd || _usd(s.earned);
  document.getElementById('eAvail').textContent = s.available_usd || _usd(s.available);
  document.getElementById('ePending').textContent = s.pending_usd || _usd(s.pending);
  document.getElementById('eWithdrawn').textContent = s.withdrawn_usd || _usd(s.withdrawn);
  ['eTotalUsd', 'eAvailUsd', 'ePendingUsd', 'eWithdrawnUsd'].forEach(id => {
    const n = document.getElementById(id); if (n) n.textContent = '';
  });

  const bt = document.getElementById('byToolList');
  bt.innerHTML = '';
  if (!d.tools || !d.tools.length) {
    bt.innerHTML = '<p class="profile-note">' + _msg('earn_tool_none') + '</p>';
  } else {
    for (const t of d.tools) {
      const row = document.createElement('div');
      row.className = 'wd-row';
      row.innerHTML = '<span class="wd-tool"></span><strong class="wd-amt"></strong>';
      row.querySelector('.wd-tool').textContent = t.tool;
      row.querySelector('.wd-amt').textContent = '+' + _usd(t.earned);
      bt.appendChild(row);
    }
  }

  const rl = document.getElementById('revLines');
  rl.innerHTML = '';
  _pager(document.getElementById('revPager'), 'rev', 'rev_page', d.rev_page || 1, d.rev_total || 0, 50);
  const rnone = document.getElementById('revNone');
  if (!d.revenue_lines || !d.revenue_lines.length) { rnone.classList.remove('hidden'); }
  else {
    rnone.classList.add('hidden');
    for (const ln of d.revenue_lines) {
      const row = document.createElement('div');
      row.className = 'wd-row';
      const t = document.createElement('span');
      t.textContent = ln.tool + ' · ' + new Date(ln.ts * 1000).toLocaleString();
      const amt = document.createElement('strong');
      amt.className = 'wd-amt';
      amt.textContent = '+' + _usd(ln.delta);
      row.append(t, amt);
      rl.appendChild(row);
    }
  }

  const h = document.getElementById('earnHist');
  h.innerHTML = '';
  _pager(document.getElementById('wdPager'), 'wd', 'wd_page', d.wd_page || 1, d.wd_total || 0, 20);
  const none = document.getElementById('earnNone');
  if (!d.withdrawals || !d.withdrawals.length) { none.classList.remove('hidden'); }
  else {
    none.classList.add('hidden');
    for (const w of d.withdrawals) {
      const row = document.createElement('div');
      row.className = 'wd-row';
      const st = document.createElement('span');
      st.className = 'wd-st st-' + w.status;
      st.textContent = _msg('earn_status_' + w.status);
      const amt = document.createElement('strong');
      amt.textContent = '-' + (w.usd || _usd(w.amount));
      row.append(Object.assign(document.createElement('span'), { textContent: '#' + w.id }), st, amt);
      h.appendChild(row);
    }
  }

  const hint = document.getElementById('withdrawHint');
  hint.textContent = _msg('earn_min').replace('{n}', (d.min_withdrawal_usd || '$10')) + ' · ' + _msg('earn_avail') + ' ' + (s.available_usd || '');
}

document.getElementById('openWithdraw')?.addEventListener('click', () => {
  _wModal.classList.remove('hidden');
  document.getElementById('wAmount').focus();
});
document.getElementById('closeWithdrawModal')?.addEventListener('click', () => _wModal.classList.add('hidden'));
_wModal?.addEventListener('click', e => { if (e.target === _wModal) _wModal.classList.add('hidden'); });

document.getElementById('submitWithdraw')?.addEventListener('click', async () => {
  const btn = document.getElementById('submitWithdraw');
  const usd = parseFloat(document.getElementById('wAmount').value) || 0;
  const note = document.getElementById('wNote').value.trim();
  btn.disabled = true;
  const r = await fetch('/api/earnings/withdraw', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ usd, note })
  });
  const d = await r.json();
  btn.disabled = false;
  if (d.ok) { _wModal.classList.add('hidden'); loadEarnings(); return; }
  alert(_errm(d.error));
});

loadEarnings();
