// ── Publish page (/account/tools/publish) — standalone form: create + edit.
// 职责: 表单状态/参数行编辑器/族勾选/校验/提交。列表逻辑归 tools.js, 不共享。
"use strict";

const _editId = new URLSearchParams(location.search).get("id") || "";
const _pubTool = window.__PUB_TOOL__ || null;

// ── helpers (single source: common.js _msg/_errm) ──
const _curLang = () => (document.documentElement.lang || "en").slice(0, 2);

// ── 场景格式族 + exact formats 勾选器 (4C-2: family=taxonomy, accept=exact
// executable contract)。唯一事实源 = 服务端注入 window.__FORMAT_FAMILIES__,
// 派生自 governance/formats.py; 本文件不维护任何 ext/MIME 镜像 ──
const FORMAT_MANIFEST = window.__FORMAT_FAMILIES__ || { order: [], families: {} };
const FAM_KEYS = FORMAT_MANIFEST.order;
const _famI18n = k => "family_" + k;
const _famExts = k => (FORMAT_MANIFEST.families[k]?.exts) || [];
// 摘要明细: 直接从 manifest exts 生成 (.jpg → JPG), 不手写七族清单
const _extLabel = e => String(e || "").replace(/^\./, "").toUpperCase();
const _famDetail = k => _famExts(k).map(_extLabel).join(" ");

// 选择状态 (单点): selectedFam=当前族, selectedExts=该族下选中的 canonical ext
let selectedFam = null;
let selectedExts = new Set();

function renderFamGrid(selFam, selExts, isCreate) {
  // isCreate: 新建态默认首族 (image) 且全选; 编辑态按回填数据;
  // 编辑遇到族外 accept token → selFam=null 受控空态 (不默认 image)。
  if (isCreate) {
    selFam = FAM_KEYS[0];
    selExts = _famExts(selFam);
  }
  selectedFam = selFam || null;
  selectedExts = new Set(selExts || []);
  const grid = document.getElementById("famGrid");
  grid.innerHTML = FAM_KEYS.map(k =>
    '<label class="fam-chip" title="' + _famDetail(k) + '"><input type="radio" name="fam" value="' + k + '"' +
    (k === selectedFam ? " checked" : "") + ">" +
    "<span>" + _msg(_famI18n(k)) + "</span></label>").join("");
  renderExtGrid();
  updateFamSummary();
}
function renderExtGrid() {
  // exact format chips: 数据只来自当前族 manifest.exts, 选中态 = selectedExts
  const box = document.getElementById("extGrid");
  if (!selectedFam) { box.innerHTML = ""; return; }
  box.innerHTML = _famExts(selectedFam).map(e =>
    '<label class="fam-chip ext-chip"><input type="checkbox" value="' + e + '"' +
    (selectedExts.has(e) ? " checked" : "") + "><span>" + _extLabel(e) +
    "</span></label>").join("");
}
function updateFamSummary() {
  const hint = document.getElementById("famHint");
  if (!selectedFam) {
    // 受控空态: 族外 accept token, 必须由用户明确选族, 禁止静默覆盖
    hint.textContent = "";
    return;
  }
  const picked = _famExts(selectedFam).filter(e => selectedExts.has(e)).map(_extLabel);
  hint.textContent = _msg(_famI18n(selectedFam)) + ": " + picked.join(" ");
}
function collectFamilies() {
  return selectedFam || FAM_KEYS[0];   // 受控空态由 submit 拦截, 这里保持单选必选
}
function collectAccept() {
  return [...selectedExts].sort();
}
// family inference: token 是 ".pdf" 这类 canonical extension → family.exts
// 匹配。stored accept 只有 ext token 一种格式。
function famKeyFromAccept(tokens) {
  const fams = new Set();
  (tokens || []).forEach(tok => {
    const t = String(tok || "").toLowerCase();
    if (!t.startsWith(".")) return;
    for (const k of FAM_KEYS) {
      const f = FORMAT_MANIFEST.families[k];
      if (f && f.exts.includes(t)) fams.add(k);
    }
  });
  return [...fams];
}
// accept tokens → canonical ext 集合 (编辑回填)。
function extsFromAccept(tokens) {
  const out = new Set();
  (tokens || []).forEach(tok => {
    const t = String(tok || "").toLowerCase();
    if (t.startsWith(".")) out.add(t);
  });
  return [...out].sort();
}
// 族切换 / exact 勾选事件 (委托, init 绑一次)
function bindFormatPickers() {
  document.getElementById("famGrid").addEventListener("change", e => {
    if (e.target.name !== "fam") return;
    selectedFam = e.target.value;
    // 切族 = 默认全选新族 (保持"选族=全族支持"的旧产品默认)
    selectedExts = new Set(_famExts(selectedFam));
    renderExtGrid();
    updateFamSummary();
  });
  document.getElementById("extGrid").addEventListener("change", e => {
    const v = e.target.value;
    if (e.target.checked) selectedExts.add(v); else selectedExts.delete(v);
    updateFamSummary();
  });
}

// ── 多语言行 (label/desc 附加语言) ──
const SITE_LANGS = [["en","English"],["zh","中文"],["ja","日本語"],["ko","한국어"],["es","Español"],["pt","Português"],["ru","Русский"],["ar","العربية"],["de","Deutsch"],["fr","Français"]];
function langRowHtml() {
  const cur = _curLang();
  const opts = SITE_LANGS.filter(([c]) => c !== cur)
    .map(([c, n]) => '<option value="' + c + '">' + n + "</option>").join("");
  return '<div class="lang-row">' +
    '<select class="lang-pick">' + opts + "</select>" +
    '<input class="lang-name" maxlength="40" placeholder="' + _msg("mytools_ph_label") + '">' +
    '<input class="lang-desc" maxlength="200" placeholder="' + _msg("mytools_ph_desc") + '">' +
    '<button type="button" class="lang-del" aria-label="✕">✕</button></div>';
}
function collectLangDict() {
  const out = {}; const cur = _curLang();
  document.querySelectorAll("#langRows .lang-row").forEach(row => {
    const lang = row.querySelector(".lang-pick").value;
    const name = row.querySelector(".lang-name").value.trim();
    const desc = row.querySelector(".lang-desc").value.trim();
    if (lang === cur) return;
    // the billing fixc 删除送达: 行存在即"提及"—— 空值显式删除该 locale (tombstone ""),
    // 未提及 = 行不存在。服务端 patch_locale 空值=删键, 与本协议闭环。
    if (lang) {
      out[lang] = name;                      // 空 name = 删除该 label locale
      out[lang + "__desc"] = desc;           // 空 desc = 删除该 desc locale
      _deletedLangs.delete(lang);
    }
  });
  // 行被移除的附加语言 = 显式删除 (tombstone 空串送达, 与"从未提及"可区分)
  _deletedLangs.forEach(lg => {
    if (lg !== cur) { out[lg] = ""; out[lg + "__desc"] = ""; }
  });
  return out;
}

// ── 参数行编辑器 (四型: enum/text/number/boolean; 服务端镜像校验) ──
const PARAM_TYPES = [
  ["enum", "pub_pt_enum"], ["text", "pub_pt_text"],
  ["number", "pub_pt_number"], ["boolean", "pub_pt_boolean"],
];
const KEY_RE = /^[a-z][a-z0-9_]{0,31}$/;
// 用户数据入 HTML 统一转义 (app.js 的五字符 escapeHtml; 发布页脚本在
// app.js 之后加载) — 回填的 key/label/placeholder/default/option value
// 与文本全部经它, 禁止只转引号的局部写法。
const _esc = s => window.escapeHtml(String(s == null ? "" : s));
// LocalizedText 编辑回填 (the billing fix): 只经 canonical resolveLabel 读 dict —
// 禁止 Object.values()[0] / 把 dict 当 string。服务端 merge 负责 patch 语义。
const _ltBack = v => window.resolveLabel ? window.resolveLabel(v) : (typeof v === "string" ? v : "");
// 显式删除的附加语言 (the billing fixc): 用户移除 lang-row 或清空输入 → 记 locale,
// 提交时以空串 tombstone 送达服务端 (与"从未提及"可区分)。
const _deletedLangs = new Set();
let _initLabel = "", _initDesc = "";
// 语言行删除单点 (the billing fixd): refill 回填行 / addLangRow 新增行共用 —
// 记 locale 进 _deletedLangs (tombstone) 后移除行
function bindLangRowDelete(row) {
  row.querySelector(".lang-del").addEventListener("click",
    e => {
      const lg = row.querySelector(".lang-pick").value;
      if (lg) _deletedLangs.add(lg);   // 行删除 = 显式删除该语言
      row.remove();
    });
}
let _paramSeq = 0;

function paramRowHtml(p) {
  const id = "pr" + (++_paramSeq);
  const typeOpts = PARAM_TYPES.map(([v, k]) =>
    `<option value="${v}"${p.type === v ? " selected" : ""}>${_msg(k)}</option>`).join("");
  return `<div class="param-row" data-pid="${id}">
    <div class="param-row-head">
      <input class="pr-key" placeholder="style" maxlength="32" value="${_esc(p.key || "")}">
      <select class="pr-type">${typeOpts}</select>
      <input class="pr-label" maxlength="80" placeholder="${_msg("pub_param_label_ph")}" value="${_esc(_ltBack(p.label))}">
      <label class="pr-req"><input type="checkbox" class="pr-required"${p.required ? " checked" : ""}><span>${_msg("pub_param_required")}</span></label>
      <button type="button" class="pr-del" aria-label="✕">✕</button>
    </div>
    <div class="param-row-body"></div>
    <em class="pub-err" data-err="param_${id}"></em>
  </div>`;
}

function paramBodyHtml(type, p) {
  p = p || {};
  if (type === "enum") {
    const opts = (p.options || []).map(o =>
      `<div class="pr-opt"><input class="pr-opt-val" placeholder="warm" maxlength="32" value="${_esc(o.value || "")}">` +
      `<input class="pr-opt-label" maxlength="40" placeholder="${_msg("pub_param_optlabel_ph")}" value="${_esc(_ltBack(o.label))}">` +
      `<button type="button" class="pr-opt-del">✕</button></div>`).join("");
    return `<div class="pr-opts">${opts}</div>
      <div class="pr-enum-foot">
        <button type="button" class="btn btn-secondary pr-opt-add">+ ${_msg("pub_add_param_option")}</button>
        <label class="pr-default-wrap"><span>${_msg("pub_param_default")}</span>
        <select class="pr-default">${(p.options || [{ value: "" }]).map(o => `<option value="${_esc(o.value)}"${p.default === o.value ? " selected" : ""}>${_esc(o.value)}</option>`).join("")}</select></label>
      </div>`;
  }
  if (type === "number") {
    return `<div class="pr-inline">
      <label><span>min</span><input class="pr-min" type="number" step="any" value="${p.min != null ? _esc(p.min) : ""}"></label>
      <label><span>max</span><input class="pr-max" type="number" step="any" value="${p.max != null ? _esc(p.max) : ""}"></label>
      <label><span>${_msg("pub_param_default")}</span><input class="pr-default-n" type="number" step="any" value="${p.default != null ? _esc(p.default) : ""}"></label>
    </div>`;
  }
  if (type === "boolean") {
    return `<label class="pr-default-wrap"><span>${_msg("pub_param_default")}</span>
      <select class="pr-default-b"><option value="">—</option><option value="true"${p.default === true ? " selected" : ""}>true</option><option value="false"${p.default === false ? " selected" : ""}>false</option></select></label>`;
  }
  return `<div class="pr-inline">
    <label class="pr-ph-wrap"><span>placeholder</span><input class="pr-ph" maxlength="80" value="${_esc(_ltBack(p.placeholder))}"></label>
    <label><span>${_msg("pub_param_default")}</span><input class="pr-default-t" maxlength="200" value="${_esc(p.default || "")}"></label>
  </div>`;
}

function bindParamRow(row) {
  const typeSel = row.querySelector(".pr-type");
  const body = row.querySelector(".param-row-body");
  typeSel.addEventListener("change", () => {
    // 类型切换: 重建空 body (不保留旧类型的值)
    body.innerHTML = paramBodyHtml(typeSel.value, {});
    bindParamBody(row);
  });
  row.querySelector(".pr-del").addEventListener("click", () => {
    row.remove(); refreshParamNone();
  });
  bindParamBody(row);
}

function bindParamBody(row) {
  const body = row.querySelector(".param-row-body");
  const typeSel = row.querySelector(".pr-type");
  const type = typeSel.value;
  if (type === "enum") {
    const optsBox = body.querySelector(".pr-opts");
    const defSel = body.querySelector(".pr-default");
    const addOpt = () => {
      const div = document.createElement("div");
      div.className = "pr-opt";
      div.innerHTML = `<input class="pr-opt-val" placeholder="warm" maxlength="32">` +
        `<input class="pr-opt-label" maxlength="40" placeholder="${_msg("pub_param_optlabel_ph")}">` +
        `<button type="button" class="pr-opt-del">✕</button>`;
      div.querySelector(".pr-opt-del").addEventListener("click", () => div.remove());
      optsBox.appendChild(div);
    };
    body.querySelector(".pr-opt-add").addEventListener("click", addOpt);
    optsBox.querySelectorAll(".pr-opt-del").forEach(b =>
      b.addEventListener("click", e => e.target.closest(".pr-opt").remove()));
  }
}

function refreshParamNone() {
  const none = document.getElementById("paramNone");
  none.classList.toggle("hidden", document.querySelectorAll(".param-row").length > 0);
}

function addParamRow(p) {
  const box = document.getElementById("paramRows");
  box.insertAdjacentHTML("beforeend", paramRowHtml(p || { type: "enum" }));
  const row = box.lastElementChild;
  const body = row.querySelector(".param-row-body");
  const typeSel = row.querySelector(".pr-type");
  // 首构建带声明数据 (bindParamRow 只建空壳; 编辑回填需要 options/default 落位)
  body.innerHTML = paramBodyHtml(typeSel.value, p || {});
  bindParamRow(row);
  refreshParamNone();
}

function collectParams(preview) {
  // 组装 params 数组; 提交态校验失败返回 null 并行内标错。
  // preview=true (实时预览): 同一组装, 跳过无效行, 不标错。
  let ok = true;
  const rows = [...document.querySelectorAll(".param-row")];
  if (!preview) rows.forEach(r => { const e = r.querySelector(".pub-err"); if (e) e.textContent = ""; });
  const seen = new Set();
  const out = [];
  for (const row of rows) {
    const key = row.querySelector(".pr-key").value.trim();
    const err = msg => { if (!preview) row.querySelector(".pub-err").textContent = _msg(msg); ok = false; };
    if (!key) { err("pub_param_key_req"); continue; }
    if (!KEY_RE.test(key)) { err("bad_param_key"); continue; }
    if (seen.has(key)) { err("bad_param_key"); continue; }
    seen.add(key);
    const type = row.querySelector(".pr-type").value;
    const label = row.querySelector(".pr-label").value.trim() || key;
    const required = row.querySelector(".pr-required").checked;
    const item = { key, type, label, required };
    if (type === "enum") {
      const options = [];
      const vals = new Set();
      let bad = false;
      row.querySelectorAll(".pr-opt").forEach(o => {
        const v = o.querySelector(".pr-opt-val").value.trim();
        if (!v || vals.has(v)) { bad = true; return; }
        vals.add(v);
        const l = o.querySelector(".pr-opt-label").value.trim();
        options.push({ value: v, label: l || v });
      });
      if (bad || !options.length) { err("bad_param_options"); continue; }
      item.options = options;
      const def = row.querySelector(".pr-default").value;
      if (def && vals.has(def)) item.default = def;
    } else if (type === "number") {
      const num = s => s.value.trim() === "" ? null : parseFloat(s.value);
      const mn = num(row.querySelector(".pr-min"));
      const mx = num(row.querySelector(".pr-max"));
      const df = num(row.querySelector(".pr-default-n"));
      if (mn != null) item.min = mn;
      if (mx != null) item.max = mx;
      if (mn != null && mx != null && mn > mx) { err("bad_param_default"); continue; }
      if (df != null) item.default = df;
    } else if (type === "boolean") {
      const dv = row.querySelector(".pr-default-b").value;
      if (dv === "true") item.default = true;
      else if (dv === "false") item.default = false;
    } else {
      const ph = row.querySelector(".pr-ph").value.trim();
      if (ph) item.placeholder = ph;
      const df = row.querySelector(".pr-default-t").value.trim();
      if (df) item.default = df;
    }
    out.push(item);
  }
  return (ok || preview) ? out : null;
}

// ── 输入方式: 文件 / 纯文本 (files_max = 0, 无文件位, 无格式声明) ──
function inputMode() {
  const el = document.querySelector('input[name="fInputMode"]:checked');
  return el ? el.value : "files";
}
function applyInputMode() {
  const text = inputMode() === "text";
  document.querySelectorAll(".pub-files-only").forEach(el => el.classList.toggle("hidden", text));
  document.getElementById("pubTextHint").classList.toggle("hidden", !text);
}

// ── 行内错误 (服务端码 → 字段定位) ──
function showErrors(errMap) {
  document.querySelectorAll(".pub-err").forEach(e => { e.textContent = ""; });
  for (const [field, code] of Object.entries(errMap)) {
    const slot = document.querySelector(`.pub-err[data-err="${field}"]`);
    if (slot) slot.textContent = _errm(code);
    else if (field.startsWith("param_")) {
      const msg = _errm(code);
      const slot2 = document.querySelector(`.pub-err[data-err="param_${field.slice(6)}"]`);
      if (slot2) slot2.textContent = msg;
    }
  }
}

// ── 提交 ──
async function submit() {
  const params = collectParams();
  if (params === null) return;   // 行内已标错
  const extra = collectLangDict();
  let label = document.getElementById("fLabel").value.trim();
  let desc = document.getElementById("fDesc").value.trim();
  if (Object.keys(extra).length) {
    const labelDict = {}; const descDict = {};
    for (const [k, v] of Object.entries(extra)) {
      if (k.endsWith("__desc")) descDict[k.slice(0, -6)] = v;
      else labelDict[k] = v;
    }
    // cur locale 三态 (the billing fixd):
    //   值 == 回填初值 → 未修改, 不并入 (旧 dict 该 locale 原样保留)
    //   值 != 初值非空 → {cur: 新值}
    //   值 != 初值空串 → {cur: ""} tombstone (显式删除该 locale)
    if (label !== _initLabel) labelDict[_curLang()] = label;
    if (desc !== _initDesc) descDict[_curLang()] = desc;
    if (Object.keys(labelDict).length > 1 || Object.keys(descDict).length) {
      label = Object.keys(labelDict).length ? labelDict : label;
      desc = Object.keys(descDict).length ? descDict : desc;
    }
  }
  const isEditPatch = !!_editId;   // Web PATCH: 空 desc = 显式删除当前 locale
  // 已存凭证的编辑态: 勾选"移除认证凭证" = 提交两个空串 (服务端
  // build_http_binding 单点解释为清除); 未勾选且输入框为空 = 不提及, 保留旧凭证
  const removeAuth = isEditPatch
    && !document.getElementById("pubRemoveAuthRow").classList.contains("hidden")
    && document.getElementById("fRemoveAuth").checked;
  // 4C-2: 受控空态 (族外 accept token) / 取消到 0 个 exact format → 拦截,
  // 禁止在未明确选择时保存覆盖原 contract。
  const textOnly = inputMode() === "text";
  if (!textOnly && (!selectedFam || collectAccept().length === 0)) {
    showErrors({ accept: "bad_format_family" });
    return;
  }
  const fmin = textOnly ? 0 : parseInt(document.getElementById("fFilesMin").value, 10);
  const fmax = textOnly ? 0 : parseInt(document.getElementById("fFilesMax").value, 10);
  if (!textOnly && !(fmin >= 0 && fmax >= 1 && fmin <= fmax && fmax <= 20)) {
    showErrors({ files_max: "bad_files_range" });
    return;
  }
  if (!document.querySelectorAll(".pub-outputs-checks input:checked").length) {
    showErrors({ outputs: "bad_outputs" });
    return;
  }
  const body = {
    tool_id: document.getElementById("fToolId").value.trim(),
    label,
    desc: (desc || (!isEditPatch ? undefined : "")),
    url: document.getElementById("fUrl").value.trim(),
    timeout_s: parseInt(document.getElementById("fTimeout").value, 10) || 60,
    format_families: textOnly ? undefined : collectFamilies(),
    accept: textOnly ? undefined : collectAccept(),
    files_min: fmin,
    files_max: fmax,
    outputs: ["text", "json", "file"].filter((k) => {
      const el = document.getElementById("fOut" + k.charAt(0).toUpperCase() + k.slice(1));
      return el && el.checked;
    }),
    auth_header: removeAuth ? "" : (document.getElementById("fAuthHeader").value.trim() || undefined),
    auth_token: removeAuth ? "" : (document.getElementById("fAuthToken").value.trim() || undefined),
    cost: parseInt(document.getElementById("fCost").value, 10) || 0,
    params: params.length ? params : undefined,
  };
  // 新工具默认 unlisted (作者成功运行一次后才能公开); 编辑态不动可见性
  // (防公开工具被编辑静默降级)
  const editing = !!_editId;
  // 编辑态 tool_id immutable: 去掉 body.tool_id, 走 URL 路径定位
  if (editing) delete body.tool_id;
  const r = await fetch("/api/usertools" + (editing ? "/" + _editId : ""), {
    method: editing ? "PATCH" : "POST",
    headers: Object.assign({ "Content-Type": "application/json" },
      editing ? { "X-518-Edit-Locale": _curLang() } : null),
    body: JSON.stringify(body),
  });
  const d = await r.json();
  if (d.ok) {
    if (editing) location.href = "/account/tools";
    // 新发布默认不公开: 直接落到工具页 (作者可运行一次, 顶部提示如何公开)
    else location.href = "/tool/" + encodeURIComponent(d.tool_key);
    return;
  }
  // 服务端错误码 → 字段定位
  const map = {
    bad_url: "url", https_required: "url", url_taken: "url",
    bad_tool_id: "tool_id", tool_id_taken: "tool_id",
    bad_label: "label", bad_desc: "desc",
    bad_accept: "accept", bad_format_family: "accept",
    bad_files_min: "files_min", bad_files_max: "files_max",
    bad_files_range: "files_max", bad_outputs: "outputs",
    bad_cost: "cost", too_many_tools: "tool_id",
  };
  if (map[d.error]) { showErrors({ [map[d.error]]: d.error }); return; }
  if (String(d.error).startsWith("bad_param")) { alert(_errm(d.error)); return; }
  alert(_errm(d.error));
}

// ── 编辑回填 ──
function refill(t) {
  _deletedLangs.clear();
  document.getElementById("fUrl").value = t.url || "";
  document.getElementById("fLabel").value = _ltBack(t.label);
  // the billing fixc: 记录主框回填初值 (= resolver fallback) — 提交时值未变 → cur locale
  // 视为未修改, 不并入提交 dict (服务端 guard 的前端对应)
  _initLabel = document.getElementById("fLabel").value;
  _initDesc = document.getElementById("fDesc").value = _ltBack(t.desc);
  document.getElementById("fToolId").value = t.tool_id;
  document.getElementById("fToolId").readOnly = true;      // id immutable
  document.getElementById("fCost").value = t.cost != null ? t.cost : 0;
  document.getElementById("fTimeout").value = t.timeout_s || 60;
  // 4C-2 编辑回填:
  // 完全未知 accept → famKey 为空 → 受控空态 (不默认 image, 不覆盖原声明)。
  const accExts = extsFromAccept(t.accept);
  const fams = famKeyFromAccept(t.accept);
  const textOnly = t.files_max === 0;
  document.querySelector('input[name="fInputMode"][value="' + (textOnly ? "text" : "files") + '"]').checked = true;
  // 纯文本工具切回文件输入时: 族网格按新建态默认, 窗口回到 1..1
  renderFamGrid(fams[0] || null, accExts, textOnly);
  document.getElementById("fFilesMin").value = textOnly ? 1 : t.files_min;
  document.getElementById("fFilesMax").value = textOnly ? 1 : t.files_max;
  applyInputMode();
  ["text", "json", "file"].forEach((k) => {
    const el = document.getElementById("fOut" + k.charAt(0).toUpperCase() + k.slice(1));
    if (el) el.checked = (t.outputs || ["file"]).indexOf(k) !== -1;
  });
  // 多语言回填
  const rows = {};
  const eat = (d, key) => {
    if (!d || typeof d !== "object") return;
    Object.entries(d).forEach(([lg, v]) => {
      if (lg === _curLang() || !v) return;
      if (key === "fLabel") (rows[lg] = rows[lg] || {}).name = v;
      else (rows[lg] = rows[lg] || {}).desc = v;
    });
  };
  eat(t.label, "fLabel"); eat(t.desc, "fDesc");
  Object.entries(rows).forEach(([lg, kv]) => {
    document.getElementById("langRows").insertAdjacentHTML("beforeend", langRowHtml());
    const row = document.querySelector("#langRows .lang-row:last-child");
    row.querySelector(".lang-pick").value = lg;
    if (kv.name) row.querySelector(".lang-name").value = kv.name;
    if (kv.desc) row.querySelector(".lang-desc").value = kv.desc;
  });
  document.querySelectorAll("#langRows .lang-row").forEach(bindLangRowDelete);
  // 参数回填
  (t.params || []).forEach(p => addParamRow(p));
  refreshParamNone();
  document.getElementById("pubTitle").textContent = _msg("pub_edit_title").replace("{id}", t.tool_id);
  document.getElementById("pubSubmit").textContent = _msg("pub_save");
  // 编辑态已有凭证: 显示"移除认证凭证"勾选项 (新建态保持隐藏)
  document.getElementById("pubRemoveAuthRow").classList.toggle("hidden", !t.has_auth);
}

// ── init ──
function initLangRowDel() {
  document.getElementById("addLangRow").addEventListener("click", () => {
    const box = document.getElementById("langRows");
    box.insertAdjacentHTML("beforeend", langRowHtml());
    bindLangRowDelete(box.lastElementChild);
  });
}

function init() {
  initLangRowDel();
  bindFormatPickers();
  renderFamGrid(null, null, true);   // 新建态: 默认首族 + 全选
  document.querySelectorAll('input[name="fInputMode"]').forEach(r =>
    r.addEventListener("change", applyInputMode));
  applyInputMode();
  document.getElementById("addParam").addEventListener("click", () => addParamRow());
  document.getElementById("pubSubmit").addEventListener("click", submit);
  document.getElementById("pubCancel").addEventListener("click",
    () => { location.href = "/account/tools"; });
  if (_pubTool) refill(_pubTool);
  refreshParamNone();
}

init();

// ── live preview: mount the real composer from the current form state ──
(function () {
  var mount = document.getElementById("pubPreviewMount");
  if (!mount || !window.ToolInput) return;
  var ctrl = null;
  var refresh = function () {
    var label = document.getElementById("fLabel").value.trim();
    var textOnly = inputMode() === "text";
    var fmin = parseInt(document.getElementById("fFilesMin").value, 10);
    var fmax = parseInt(document.getElementById("fFilesMax").value, 10);
    var fminOk = textOnly ? 0 : (Number.isFinite(fmin) && fmin >= 0 ? fmin : 1);
    var fmaxOk = textOnly ? 0 : (Number.isFinite(fmax) && fmax >= 1 ? fmax : 1);
    var outs = [];
    if (document.getElementById("fOutText").checked) outs.push("text");
    if (document.getElementById("fOutJson").checked) outs.push("json");
    if (document.getElementById("fOutFile").checked) outs.push("file");
    var accept = textOnly ? [] : [...selectedExts].sort();
    // 预览与提交同一参数组装 (真实 options/default/min/max/placeholder)
    var params = collectParams(true);
    var tool = {
      id: "preview", label: label || (_msg("mytools_ph_label")),
      icon: "wand", files_min: fminOk, files_max: fmaxOk,
      accept: accept.length ? accept : null,
      outputs: outs.length ? outs : ["file"],
      params: params, cost: parseInt(document.getElementById("fCost").value, 10) || 0,
    };
    if (!ctrl) {
      ctrl = window.ToolInput.mount(mount, { tool: tool });
    } else {
      ctrl.setTool(tool);
    }
  };
  document.getElementById("pubForm").addEventListener("input", refresh);
  document.getElementById("pubForm").addEventListener("change", refresh);
  refresh();
})();
