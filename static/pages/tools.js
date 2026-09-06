// ── My publications (C2C supply): card list + modal submit + icon grid ──
const _toolList = document.getElementById('toolList');
let _editKey = null;
const _modal = document.getElementById('toolModal');

// (icon/category picker removed — icons fixed server-side)

function openModal() {
  _modal.classList.remove('hidden');
  renderFamGrid([]);   // 新建: 族全不勾 = 全部格式
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

// ── 场景格式族勾选器 (0930 范式: 声明即契约) ──
// 词表单源在服务端 governance/formats.py; 此处静态镜像 FAMILY_ORDER+标签键,
// 服务端漂移时提交校验 (bad_format_family) 兜底。
// 族明细 = 悬停 title (格式归类明确性, 用户问"数据/代码囊括什么"直接可查)。
const FAM_KEYS = ["image", "document", "archive", "data", "code"];
const FAM_I18N = {image: "family_image", document: "family_document",
                  archive: "family_archive", data: "family_data", code: "family_code"};
const FAM_DETAIL = {
  image: "JPG PNG GIF WebP BMP HEIC HEIF TIFF AVIF ICO",
  document: "PDF TXT CSV MD RTF DOC DOCX XLS XLSX PPT PPTX ODT ODS ODP",
  archive: "ZIP RAR 7Z GZ TAR BZ2 XZ TGZ",
  data: "JSON XML YAML YML",
  code: "PY JS TS HTML CSS SVG GO RS JAVA C CPP H RB PHP SH SQL",
};
function renderFamGrid(preselected) {
  const grid = document.getElementById('famGrid');
  if (!grid) return;
  grid.innerHTML = FAM_KEYS.map(k =>
    '<label class="fam-chip" title="' + FAM_DETAIL[k] + '"><input type="checkbox" value="' + k + '"' +
    (preselected && preselected.includes(k) ? ' checked' : '') + '>' +
    '<span>' + _msg(FAM_I18N[k]) + '</span></label>').join('');
  const hint = document.createElement('span');
  hint.className = 'fam-hint';
  hint.textContent = FAM_KEYS.map(k => _msg(FAM_I18N[k]) + ': ' + FAM_DETAIL[k]).join(' · ');
  grid.appendChild(hint);
}
function collectFamilies() {
  const checked = [...document.querySelectorAll('#famGrid input:checked')].map(el => el.value);
  return checked.length ? checked : undefined;   // 不勾 = 全部 (accept=None 语义)
}
function famKeyFromMimes(mimes) {
  // 存量 MIME 声明 → 族集合 (镜像服务端映射; 未知 MIME 忽略)
  const EXT_BY_MIME = {
    "image/jpeg": "image", "image/png": "image", "image/gif": "image", "image/webp": "image",
    "image/bmp": "image", "image/tiff": "image", "image/heic": "image", "image/avif": "image",
    "image/x-icon": "ico", "image/vnd.microsoft.icon": "ico", "image/svg+xml": "code",
    "application/pdf": "document", "text/plain": "document", "text/csv": "document",
    "application/rtf": "document", "application/msword": "document",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "document",
    "application/vnd.ms-excel": "document",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "document",
    "application/vnd.ms-powerpoint": "document",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": "document",
    "application/zip": "archive", "application/x-7z-compressed": "archive",
    "application/gzip": "archive", "application/x-tar": "archive",
    "application/json": "data", "application/xml": "data", "text/xml": "data",
    "application/yaml": "data",
    "text/x-python": "code", "text/javascript": "code", "application/javascript": "code",
    "text/html": "code", "text/css": "code",
  };
  const fams = new Set();
  (mimes || []).forEach(m => { const f = EXT_BY_MIME[m]; if (f) fams.add(f); });
  return [...fams];
}

// ── edit: refill the submit modal in PATCH mode ──
function _openEdit(t) {
  _editKey = t.tool_id;
  // 行区复位: 编辑回填前清空语言行 (防上次编辑累积)
  document.getElementById('langRows').innerHTML = '';
  // 编辑回填: label/desc dict → 主框(当前语言) + 语言行合并回填 (en 优先回落)
  const lang = _curLang();
  const rows = {};  // lg → {name, desc}
  const eat = (d, key) => {
    if (typeof d === 'string') document.getElementById(key).value = d;
    else if (d && typeof d === 'object') {
      document.getElementById(key).value = d[lang] || d.en || Object.values(d)[0] || '';
      Object.entries(d).forEach(([lg, v]) => {
        if (lg === lang || !v) return;
        if (key === 'fLabel') (rows[lg] = rows[lg] || {}).name = v;
        else (rows[lg] = rows[lg] || {}).desc = v;
      });
    }
  };
  eat(t.label, 'fLabel');
  eat(t.desc, 'fDesc');
  Object.entries(rows).forEach(([lg, kv]) => {
    document.getElementById('addLangRow')?.click();
    const row = document.querySelector('#langRows .lang-row:last-child');
    if (row) {
      row.querySelector('.lang-pick').value = lg;
      if (kv.name) row.querySelector('.lang-name').value = kv.name;
      if (kv.desc) row.querySelector('.lang-desc').value = kv.desc;
    }
  });
  document.getElementById('fToolId').value = t.tool_id;
  document.getElementById('fToolId').disabled = true;  // id immutable
  document.getElementById('fUrl').value = (t.url || '');
  document.getElementById('fCost').value = t.cost != null ? t.cost : 0;
  document.getElementById('fTimeout') && (document.getElementById('fTimeout').value = t.timeout_s || 60);
  document.getElementById('fOut') && (document.getElementById('fOut').value = t.output_filename || '');
  document.getElementById('fOutCt') && (document.getElementById('fOutCt').value = t.output_content_type || '');
  renderFamGrid(famKeyFromMimes(t.accept));   // 编辑: 存量 MIME 声明映射回族勾选
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

// label 解析 (str|dict, 同 app.js resolveLabel 回落链 — 此页无 app.js 依赖)
function _rl(label) {
  if (typeof label === 'string') return label;
  if (label && typeof label === 'object') {
    const lang = document.documentElement.lang.slice(0, 2) || 'en';
    if (label[lang]) return label[lang];
    if (label.en) return label.en;
    for (const v of Object.values(label)) if (typeof v === 'string' && v) return v;
  }
  return '';
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
    name.textContent = _rl(t.label) || t.tool_key;
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

// 多语言输入行 (tool modes v3): 语言下拉(原生名, 排除当前界面语言) + 名称 + 简介
// 原文输入框 (fLabel/fDesc) = 当前语言主项; 附加语言行组装 dict。
const SITE_LANGS = [["en","English"],["zh","中文"],["ja","日本語"],["ko","한국어"],["es","Español"],["pt","Português"],["ru","Русский"],["ar","العربية"],["de","Deutsch"],["fr","Français"]];
const _curLang = () => (document.documentElement.lang || "en").slice(0, 2);
function langRowHtml() {
  const cur = _curLang();
  const opts = SITE_LANGS.filter(([c]) => c !== cur)
    .map(([c, n]) => '<option value="' + c + '">' + n + '</option>').join('');
  return '<div class="lang-row">' +
    '<select class="lang-pick">' + opts + '</select>' +
    '<input class="lang-name" maxlength="40" placeholder="' + _msg('mytools_ph_label') + '">' +
    '<input class="lang-desc" maxlength="200" placeholder="' + _msg('mytools_ph_desc') + '">' +
    '<button type="button" class="lang-del" aria-label="✕">✕</button></div>';
}
document.getElementById('addLangRow')?.addEventListener('click', () => {
  const wrap = document.getElementById('langRows');
  wrap.insertAdjacentHTML('beforeend', langRowHtml());
  wrap.lastElementChild.querySelector('.lang-del').addEventListener('click', e => e.target.closest('.lang-row').remove());
});
function collectLangDict() {
  const out = {};
  const cur = _curLang();
  document.querySelectorAll('#langRows .lang-row').forEach(row => {
    const lang = row.querySelector('.lang-pick').value;
    const name = row.querySelector('.lang-name').value.trim();
    const desc = row.querySelector('.lang-desc').value.trim();
    if (lang === cur) return;  // 当前语言走主输入框, 行选它会被主值覆盖
    if (lang && name) out[lang] = name;
    if (lang && desc) out[lang + '__desc'] = desc;   // 临时携带, 下方拆分
  });
  return out;
}

document.getElementById('submitTool').addEventListener('click', async () => {
  // 多语言组装: 附加语言行 dict + 主输入框 = 原文 (spec v3: str|dict 双形态)
  const extra = collectLangDict();
  let label = document.getElementById('fLabel').value.trim();
  let desc = document.getElementById('fDesc').value.trim();
  if (Object.keys(extra).length) {
    const labelDict = {}; const descDict = {};
    for (const [k, v] of Object.entries(extra)) {
      if (k.endsWith('__desc')) descDict[k.slice(0, -6)] = v;
      else labelDict[k] = v;
    }
    if (label) labelDict[document.documentElement.lang.slice(0,2)] = label;
    if (desc) descDict[document.documentElement.lang.slice(0,2)] = desc;
    if (Object.keys(labelDict).length > 1 || Object.keys(descDict).length) {
      label = Object.keys(labelDict).length ? labelDict : label;
      desc = Object.keys(descDict).length ? descDict : desc;
    }
  }
  const body = {
    tool_id: document.getElementById('fToolId').value.trim(),
    label: label,
    desc: desc || undefined,
    url: document.getElementById('fUrl').value.trim(),
    timeout_s: parseInt(document.getElementById('fTimeout').value, 10) || 60,
    accept: document.getElementById('fAccept') ? document.getElementById('fAccept').value.trim() || undefined : undefined,
    format_families: collectFamilies(),
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
// tool modes v3: multilang label/desc wiring
