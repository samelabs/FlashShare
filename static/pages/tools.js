// ── My publications (C2C supply side): card list management.
// 创建/编辑已迁独立发布页 (/account/tools/publish) — 本文件仅管列表。
"use strict";

const _toolList = document.getElementById('toolList');

// 4A-fix4: 自测入口收口到真实工具页 — /tool/{owner}/{tool_id}
// (t.tool_key 即 owner/tool_id)。作者以与真实用户完全相同的路径自测:
// canonical params → 选文件 → runToolFlow → /api/tools/execute →
// dispatch → gate → real endpoint。此处不复制 params renderer,
// 不再维护 upload→selftest 重复消费链。

// ── call log drawer: last 20 calls on MY tool ──
function _showCalls(btn, t) {
  let drawer = btn.closest('.usertools-item').querySelector('.calls-drawer');
  if (drawer) { drawer.remove(); return; }  // toggle
  btn.disabled = true;
  fetch('/api/usertools/' + t.tool_id + '/calls').then(r => r.json()).then(d => {
    btn.disabled = false;
    if (!d.ok) { alert(_errm(d.error)); return; }
    drawer = document.createElement('div');
    drawer.className = 'calls-drawer';
    if (!d.calls.length) {
      drawer.textContent = _msg('mytools_calls_none');
    } else {
      d.calls.forEach(c => {
        const line = document.createElement('div');
        line.className = 'calls-line';
        const dt = new Date(c.created_at * 1000);
        line.innerHTML = '<span class="calls-time"></span><span class="calls-st ' + (c.status === 'ok' ? 'ok' : 'bad') + '"></span><span class="calls-ms"></span>';
        line.querySelector('.calls-time').textContent = dt.toISOString().slice(0, 16).replace('T', ' ');
        line.querySelector('.calls-st').textContent = c.status;
        line.querySelector('.calls-ms').textContent = c.ms != null ? c.ms + 'ms' : '';
        drawer.appendChild(line);
      });
    }
    btn.closest('.usertools-item').appendChild(drawer);
  });
}

function _delBtn(toolId, toolKey) {
  // two-state confirm (site pattern, no window.confirm)
  const b = document.createElement('button');
  b.type = 'button'; b.className = 'btn-secondary';
  let armed = false, timer = 0;
  b.textContent = _msg('delete');
  b.addEventListener('click', async () => {
    if (!armed) {
      armed = true; b.classList.add('danger'); b.textContent = _msg('mytools_confirm_del') + '?';
      timer = setTimeout(() => { armed = false; b.classList.remove('danger'); b.textContent = _msg('delete'); }, 3000);
      return;
    }
    clearTimeout(timer); b.disabled = true;
    const r = await fetch('/api/usertools/' + toolId, { method: 'DELETE' });
    const d = await r.json();
    if (d.ok) loadTools(); else { b.disabled = false; alert(_errm(d.error)); }
  });
  return b;
}

async function loadTools() {
  const d = await (await fetch('/api/usertools')).json();
  if (!d.ok) return;
  _toolList.innerHTML = '';
  for (const t of (d.tools || [])) {
    const card = document.createElement('div');
    card.className = 'usertools-item';
    const head = document.createElement('div');
    head.className = 'usertools-head';
    const name = document.createElement('strong');
    name.textContent = window.resolveLabel(t.label) || t.tool_id;
    const key = document.createElement('span');
    key.className = 'usertools-key';
    key.textContent = t.tool_key;
    const vis = document.createElement('span');
    vis.className = 'usertools-vis v-' + t.visibility;
    vis.textContent = _msg('mytools_v_' + t.visibility);
    head.append(name, key, vis);

    const meta = document.createElement('div');
    meta.className = 'usertools-meta';
    meta.textContent = (t.calls ? t.calls.ok : 0) + ' ' + _msg('mytools_calls')
      + ' · ' + (t.enabled ? _msg('mytools_enable') : _msg('mytools_disable'));

    const actions = document.createElement('div');
    actions.className = 'tool-panel-actions';

    const visBtn = document.createElement('button');
    visBtn.type = 'button'; visBtn.className = 'btn-secondary';
    visBtn.textContent = t.visibility === 'public' ? _msg('mytools_v_unlisted') : _msg('mytools_publish');
    visBtn.addEventListener('click', async () => {
      visBtn.disabled = true;
      const next = t.visibility === 'public' ? 'unlisted' : 'public';
      const r2 = await fetch('/api/usertools/' + t.tool_id, {
        method: 'PATCH', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ visibility: next }),
      });
      const d2 = await r2.json();
      visBtn.disabled = false;
      if (d2.ok) loadTools();
      else if (d2.error === 'self_test_required') alert(_msg('mytools_selftest'));
      else alert(_errm(d2.error));
    });
    const stBtn = document.createElement('button');
    stBtn.type = 'button'; stBtn.className = 'btn-secondary';
    stBtn.textContent = _msg('mytools_run_selftest');
    // 4A-fix4: 导航到真实工具页自测 — 不再走 upload→selftest 执行链
    stBtn.addEventListener('click', () => {
      location.href = '/tool/' + t.tool_key;
    });
    const logBtn = document.createElement('button');
    logBtn.type = 'button'; logBtn.className = 'btn-secondary';
    logBtn.textContent = _msg('mytools_calls_btn');
    logBtn.addEventListener('click', () => _showCalls(logBtn, t));
    const edBtn = document.createElement('button');
    edBtn.type = 'button'; edBtn.className = 'btn-secondary';
    edBtn.textContent = _msg('mytools_edit');
    edBtn.addEventListener('click', () => { location.href = '/account/tools/publish?id=' + encodeURIComponent(t.tool_id); });
    const enBtn = document.createElement('button');
    enBtn.type = 'button';
    enBtn.className = 'btn-secondary' + (t.enabled ? ' danger' : '');
    enBtn.textContent = t.enabled ? _msg('mytools_disable') : _msg('mytools_enable');
    enBtn.addEventListener('click', async () => {
      enBtn.disabled = true;
      const r3 = await fetch('/api/usertools/' + t.tool_id, {
        method: 'PATCH', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ enabled: !t.enabled }),
      });
      const d3 = await r3.json();
      enBtn.disabled = false;
      if (d3.ok) loadTools(); else alert(_errm(d3.error));
    });
    actions.append(stBtn, logBtn, edBtn, enBtn, visBtn, _delBtn(t.tool_id, t.tool_key));

    card.append(head, meta, actions);
    _toolList.appendChild(card);
  }
  const none = document.getElementById('toolNone');
  if (none) none.classList.toggle('hidden', !!(d.tools && d.tools.length));
}

loadTools();
