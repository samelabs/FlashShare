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
  next.textContent = _msg('next_page');
  el.append(prev, mid, next);
}

async function loadEarnings() {
  let d;
  try {
    const r = await fetch('/api/earnings');
    if (!r.ok) return;
    d = await r.json();
  } catch (e) { return; }
  if (!d || !d.ok || !d.summary) return;
  const s = d.summary;
  // C1: Earnings 的资产单位就是现金 USD — 主值直接 USD, 不再以
  // credits 数字为主 + USD 小字 (消费积分属于 /account/credits)。
  document.getElementById('eTotal').textContent = s.earned_usd;
  document.getElementById('eAvail').textContent = s.available_usd;
  document.getElementById('ePending').textContent = s.pending_usd;
  document.getElementById('eWithdrawn').textContent = s.withdrawn_usd;

  // per-tool revenue breakdown
  const bt = document.getElementById('byToolList');
  bt.innerHTML = '';
  if (!d.tools || !d.tools.length) {
    bt.innerHTML = '<p class="profile-note">' + _msg('earn_tool_none') + '</p>';
  } else {
    for (const t of d.tools) {
      const row = document.createElement('div');
      row.className = 'wd-row';
      row.innerHTML = '<span class="wd-tool"></span><strong class="wd-amt"></strong>';
      row.querySelector('.wd-tool').textContent =
        (window.resolveLabel && t.label && window.resolveLabel(t.label)) || t.tool;
      row.querySelector('.wd-amt').textContent = '+' + t.earned_usd;
      bt.appendChild(row);
    }
  }

  // revenue detail lines
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
      t.textContent = ((window.resolveLabel && ln.label && window.resolveLabel(ln.label)) || ln.tool)
        + ' · ' + new Date(ln.ts * 1000).toLocaleString();
      const amt = document.createElement('strong');
      amt.className = 'wd-amt';
      amt.textContent = '+' + ln.amount_usd;
      row.append(t, amt);
      rl.appendChild(row);
    }
  }

  // withdrawal history
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
    amt.textContent = '-' + w.amount_usd;
    // request-time frozen channel from the withdrawal row itself
    const ch = document.createElement('span');
    ch.className = 'wd-ch';
    ch.textContent = (w.payout_channel_snapshot || '') +
      ' · ' + new Date(w.created_at * 1000).toLocaleString();
    row.append(Object.assign(document.createElement('span'), { textContent: '#' + w.id }), st, amt, ch);
    h.appendChild(row);
  }

    }

  _curChannel = d.payout_channel || '';
  const hint = document.getElementById('withdrawHint');
  hint.textContent = _msg('earn_min').replace('{n}', d.min_withdrawal_usd) + ' · ' + _msg('earn_avail') + ' ' + s.available_usd;
}

let _curChannel = '';
document.getElementById('openWithdraw')?.addEventListener('click', () => {
  _wModal.classList.remove('hidden');
  refreshWithdrawModal();
  document.getElementById('wAmount').focus();
});

function refreshWithdrawModal() {
  const hint = document.getElementById('withdrawHint');
  const btn = document.getElementById('submitWithdraw');
  const chan = document.getElementById('wChannel');
  if (chan) {
    chan.textContent = _curChannel
      ? _curChannel
      : _msg('earn_wd_channel_unset');
    chan.classList.toggle('unset', !_curChannel);
  }
  if (!_curChannel) {
    // a request now is guaranteed payout_channel_required — do not send it
    btn.disabled = true;
    hint.textContent = '';
    const a = document.createElement('a');
    a.href = '/account/profile';
    a.textContent = _msg('earn_no_channel');
    hint.appendChild(a);
  } else {
    btn.disabled = false;
  }
}
document.getElementById('closeWithdrawModal')?.addEventListener('click', () => _wModal.classList.add('hidden'));
_wModal?.addEventListener('click', e => { if (e.target === _wModal) _wModal.classList.add('hidden'); });

document.getElementById('submitWithdraw')?.addEventListener('click', async () => {
  const btn = document.getElementById('submitWithdraw');
  const amountUsd = document.getElementById('wAmount').value.trim();
  const note = document.getElementById('wNote').value.trim();
  btn.disabled = true;
  try {
    const r = await fetch('/api/earnings/withdraw', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ amount_usd: amountUsd, note })
    });
    const d = await r.json();
    if (d.ok) {
      _wModal.classList.add('hidden');
      const fb = document.getElementById('wdFeedback');
      if (fb) {
        fb.textContent = _msg('earn_wd_ok') + ' (#' + d.id + ')';
        fb.classList.remove('hidden');
        setTimeout(() => fb.classList.add('hidden'), 6000);
      }
      loadEarnings();
      return;
    }
    alert(_errm(d.error));
  } catch (e) {
    // 网络/解析失败也要把资金操作入口解禁, 否则按钮永久锁死
    alert(_errm());
  } finally {
    if (_curChannel) btn.disabled = false;
  }
});

loadEarnings();
