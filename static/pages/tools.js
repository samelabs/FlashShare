// ── My publications (C2C supply): card list + modal submit + icon grid ──
const _toolList = document.getElementById('toolList');
let _editKey = null;
const _modal = document.getElementById('toolModal');

// (icon/category picker removed — icons fixed server-side)

function openModal() {
  _modal.classList.remove('hidden');
  document.getElementById('fLabel').focus();
}
function closeModal() { _modal.classList.add('hidden'); _editKey = null;
  document.getElementById('fToolId').disabled = false;
  const mt = document.getElementById('modalTitle'); if (mt) mt.textContent = _msg('mytools_create');
}
document.getElementById('openToolModal').addEventListener('click', openModal);
document.getElementById('closeToolModal').addEventListener('click', closeModal);
_modal.addEventListener('click', (e) => { if (e.target === _modal) closeModal(); });
document.addEventListener('keydown', (e) => { if (e.key === 'Escape' && !_modal.classList.contains('hidden')) closeModal(); });


// ── self-test: owner picks a file, platform runs ONE real call ──
function _selfTest(btn, t) {
  const input = document.createElement('input');
  input.type = 'file';
  input.addEventListener('change', async () => {
    const f = input.files && input.files[0];
    if (!f) return;
    btn.disabled = true;
    const oldTxt = btn.textContent;
    btn.textContent = _msg('mytools_testing');
    try {
      const up = new FormData();
      up.append('file', f);
      const ru = await fetch('/api/uploads', { method: 'POST', body: up });
      const du = await ru.json();
      if (!du.ok) throw new Error(du.error || 'upload failed');
      const rs = await fetch('/api/usertools/' + t.tool_id + '/selftest', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ public_id: du.file.publicId, source_token: du.file.sourceToken }),
      });
      const ds = await rs.json();
      if (ds.ok) alert(_msg('mytools_st_ok').replace('{ms}', ds.ms || '?'));
      else {
        alert(_errm(ds.error));
      }
    } catch (e) { alert(e.message || 'selftest failed'); }
    btn.disabled = false; btn.textContent = oldTxt;
  });
  input.click();
}

// ── edit: refill the submit modal in PATCH mode ──
function _openEdit(t) {
  _editKey = t.tool_id;
  document.getElementById('fLabel').value = t.label || '';
  document.getElementById('fToolId').value = t.tool_id;
  document.getElementById('fToolId').disabled = true;  // id immutable
  document.getElementById('fUrl').value = (t.url || '');
  document.getElementById('fCost').value = t.cost != null ? t.cost : 0;
  document.getElementById('fTimeout') && (document.getElementById('fTimeout').value = t.timeout_s || 60);
  document.getElementById('fOut') && (document.getElementById('fOut').value = t.output_filename || '');
  document.getElementById('fOutCt') && (document.getElementById('fOutCt').value = t.output_content_type || '');
  document.getElementById('fAccept') && (document.getElementById('fAccept').value = (t.accept || []).join(','));
  document.getElementById('fAuthHeader') && (document.getElementById('fAuthHeader').value = t.auth_header || '');
  document.getElementById('modalTitle') && (document.getElementById('modalTitle').textContent = _msg('mytools_edit'));
  document.getElementById('toolModal').classList.remove('hidden');
}

// ── call log drawer (B3): last 20 calls on MY tool ──
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
      for (const c of d.calls) {
        const row = document.createElement('div');
        row.className = 'wd-row';
        const st = document.createElement('span');
        st.className = 'wd-st st-' + c.status;
        st.textContent = c.status;
        const info = document.createElement('span');
        info.textContent = (c.ms != null ? c.ms + 'ms' : '—') + (c.error ? ' · ' + c.error : '');
        const cost = document.createElement('strong');
        cost.textContent = String(c.cost_snapshot || 0);
        row.append(st, info, cost);
        drawer.appendChild(row);
      }
    }
    btn.closest('.usertools-item').appendChild(drawer);
  });
}

function _delBtn(toolId, key) {
  const btn = document.createElement('button');
  btn.type = 'button'; btn.className = 'btn-secondary';
  let armed = false, timer = 0;
  btn.textContent = _msg('delete');
  btn.addEventListener('click', async () => {
    if (!armed) {  // two-state confirm (site pattern, no window.confirm)
      armed = true; btn.classList.add('danger'); btn.textContent = _msg('mytools_confirm_del') + '?';
      timer = setTimeout(() => { armed = false; btn.classList.remove('danger'); btn.textContent = _msg('delete'); }, 3000);
      return;
    }
    clearTimeout(timer); btn.disabled = true;
    const r = await fetch('/api/usertools/' + toolId, { method: 'DELETE' });
    const d = await r.json();
    if (d.ok) loadTools(); else { btn.disabled = false; alert(_errm(d.error)); }
  });
  return btn;
}

async function loadTools() {
  const r = await fetch('/api/usertools');
  const d = await r.json();
  if (!d.ok) return;
  _toolList.innerHTML = '';
  for (const t of d.tools) {
    const card = document.createElement('div');
    card.className = 'usertools-item';

    const head = document.createElement('div');
    head.className = 'usertools-head';
    const ico = document.createElement('span');
    ico.className = 'usertools-ico';
    if (t.icon) ico.innerHTML = '<svg viewBox="0 0 24 24" aria-hidden="true">' + (window.__ICONS__[t.icon] || '') + '</svg>';
    const name = document.createElement('strong');
    name.textContent = t.label || t.tool_key;
    const keyEl = document.createElement('span');
    keyEl.className = 'usertools-key';
    keyEl.textContent = t.tool_key;
    const vis = document.createElement('span');
    vis.className = 'usertools-vis v-' + t.visibility;
    vis.textContent = _msg('mytools_v_' + t.visibility);
    head.append(ico, name, keyEl, vis);

    const meta = document.createElement('div');
    meta.className = 'usertools-meta';
    meta.textContent = `${_msg('mytools_price')} ${t.cost} · ` +
      `${_msg('mytools_calls')} ${t.calls.ok}/${t.calls.total}` +
      (t.calls.earned > 0 ? ` · ${_msg('mytools_earned')} +${t.calls.earned}` : '');

    const actions = document.createElement('div');
    actions.className = 'usertools-actions';
    const visBtn = document.createElement('button');
    visBtn.type = 'button'; visBtn.className = 'btn-secondary';
    const next = { private: 'unlisted', unlisted: 'public', public: 'private' }[t.visibility];
    visBtn.textContent = _msg('mytools_set_' + next);
    visBtn.addEventListener('click', async () => {
      visBtn.disabled = true;
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
    stBtn.addEventListener('click', () => _selfTest(stBtn, t));
    const logBtn = document.createElement('button');
    logBtn.type = 'button'; logBtn.className = 'btn-secondary';
    logBtn.textContent = _msg('mytools_calls_btn');
    logBtn.addEventListener('click', () => _showCalls(logBtn, t));
    const edBtn = document.createElement('button');
    edBtn.type = 'button'; edBtn.className = 'btn-secondary';
    edBtn.textContent = _msg('mytools_edit');
    edBtn.addEventListener('click', () => _openEdit(t));
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

document.getElementById('submitTool').addEventListener('click', async () => {
  const body = {
    tool_id: document.getElementById('fToolId').value.trim(),
    label: document.getElementById('fLabel').value.trim(),
    url: document.getElementById('fUrl').value.trim(),
    timeout_s: parseInt(document.getElementById('fTimeout').value, 10) || 60,
    accept: document.getElementById('fAccept').value.trim() || undefined,
    output_filename: document.getElementById('fOut').value.trim(),
    output_content_type: document.getElementById('fOutCt').value.trim() || undefined,
    auth_header: document.getElementById('fAuthHeader').value.trim() || undefined,
    auth_token: document.getElementById('fAuthToken').value.trim() || undefined,
    cost: parseInt(document.getElementById('fCost').value, 10) || 0,
    visibility: 'private',
  };
  // validation masks (mirror backend _validate_submission)
  const editing = !!_editKey;
  const r = await fetch('/api/usertools' + (editing ? '/' + _editKey : ''), {
    method: editing ? 'PATCH' : 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  const d = await r.json();
  if (d.ok) { _editKey = null; closeModal(); loadTools(); }
  else {
    alert(_errm(d.error));
  }
});


loadTools();
