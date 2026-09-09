// ── Publish page (/account/tools/publish) — standalone form: create + edit.
// 职责: 表单状态/参数行编辑器/族勾选/校验/提交。列表逻辑归 tools.js, 不共享。
"use strict";

const _editId = new URLSearchParams(location.search).get("id") || "";
const _pubTool = window.__PUB_TOOL__ || null;

// ── helpers (single source: common.js _msg/_errm) ──
const _curLang = () => (document.documentElement.lang || "en").slice(0, 2);

// ── 场景格式族勾选器 (4C-2a: 唯一事实源 = 服务端注入的 window.__FORMAT_FAMILIES__,
// 派生自 governance/formats.py; 本文件不再维护任何 ext/MIME 镜像) ──
const FORMAT_MANIFEST = window.__FORMAT_FAMILIES__ || { order: [], families: {} };
const FAM_KEYS = FORMAT_MANIFEST.order;
const _famI18n = k => "family_" + k;
// 摘要明细: 直接从 manifest exts 生成 (.jpg → JPG), 不手写七族清单
const _famDetail = k => (FORMAT_MANIFEST.families[k]?.exts || [])
  .map(e => e.replace(/^\./, "").toUpperCase()).join(" ");
function renderFamGrid(preselected) {
  const grid = document.getElementById("famGrid");
  const sel = preselected && preselected.length ? preselected[0] : FAM_KEYS[0]; // 默认 image, 不可空
  grid.innerHTML = FAM_KEYS.map(k =>
    '<label class="fam-chip" title="' + _famDetail(k) + '"><input type="radio" name="fam" value="' + k + '"' +
    (k === sel ? " checked" : "") + ">" +
    "<span>" + _msg(_famI18n(k)) + "</span></label>").join("");
  // 摘要行: 单行, 随选中族实时更新 (明细的常驻清单是文档排版, 不进表单;
  // 完整明细在 chip title 悬停)
  updateFamSummary();
  grid.addEventListener("change", updateFamSummary);
}
function updateFamSummary() {
  const sel = document.querySelector("#famGrid input:checked");
  const key = sel ? sel.value : FAM_KEYS[0];
  const hint = document.getElementById("famHint");
  hint.textContent = _msg(_famI18n(key)) + ": " + _famDetail(key);
}
function collectFamilies() {
  const sel = document.querySelector("#famGrid input:checked");
  return sel ? sel.value : FAM_KEYS[0];   // 单选必选: 无空态
}
// 通用 family inference (4C-2a): token 是 MIME → family.mimes 匹配;
// token 是 ".pdf" 这类 extension → family.exts 匹配。下一阶段切换 canonical
// extension token 时发布页无需再改 inference。本批 submit 仍只发 family key。
function famKeyFromAccept(tokens) {
  const fams = new Set();
  (tokens || []).forEach(tok => {
    const t = String(tok || "").toLowerCase();
    for (const k of FAM_KEYS) {
      const f = FORMAT_MANIFEST.families[k];
      if (!f) continue;
      if (t.startsWith(".")) {
        if (f.exts.includes(t)) fams.add(k);
      } else if (f.mimes.includes(t)) {
        fams.add(k);
      }
    }
  });
  return [...fams];
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
    // 批 2c 删除送达: 行存在即"提及"—— 空值显式删除该 locale (tombstone ""),
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
// LocalizedText 编辑回填 (批 2): 只经 canonical resolveLabel 读 dict —
// 禁止 Object.values()[0] / 把 dict 当 string。服务端 merge 负责 patch 语义。
const _ltBack = v => window.resolveLabel ? window.resolveLabel(v) : (typeof v === "string" ? v : "");
// 显式删除的附加语言 (批 2c): 用户移除 lang-row 或清空输入 → 记 locale,
// 提交时以空串 tombstone 送达服务端 (与"从未提及"可区分)。
const _deletedLangs = new Set();
let _initLabel = "", _initDesc = "";
// 语言行删除单点 (批 2d): refill 回填行 / addLangRow 新增行共用 —
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
      <input class="pr-key" placeholder="style" maxlength="32" value="${(p.key || "").replace(/"/g, "&quot;")}">
      <select class="pr-type">${typeOpts}</select>
      <input class="pr-label" maxlength="80" placeholder="${_msg("pub_param_label_ph")}" value="${_ltBack(p.label).replace(/"/g, "&quot;")}">
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
      `<div class="pr-opt"><input class="pr-opt-val" placeholder="warm" maxlength="32" value="${(o.value || "").replace(/"/g, "&quot;")}">` +
      `<input class="pr-opt-label" maxlength="40" placeholder="${_msg("pub_param_optlabel_ph")}" value="${_ltBack(o.label).replace(/"/g, "&quot;")}">` +
      `<button type="button" class="pr-opt-del">✕</button></div>`).join("");
    return `<div class="pr-opts">${opts}</div>
      <div class="pr-enum-foot">
        <button type="button" class="btn-secondary pr-opt-add">+ ${_msg("pub_add_param_option")}</button>
        <label class="pr-default-wrap"><span>${_msg("pub_param_default")}</span>
        <select class="pr-default">${(p.options || [{ value: "" }]).map(o => `<option value="${o.value}"${p.default === o.value ? " selected" : ""}>${o.value}</option>`).join("")}</select></label>
      </div>`;
  }
  if (type === "number") {
    return `<div class="pr-inline">
      <label><span>min</span><input class="pr-min" type="number" step="any" value="${p.min != null ? p.min : ""}"></label>
      <label><span>max</span><input class="pr-max" type="number" step="any" value="${p.max != null ? p.max : ""}"></label>
      <label><span>${_msg("pub_param_default")}</span><input class="pr-default-n" type="number" step="any" value="${p.default != null ? p.default : ""}"></label>
    </div>`;
  }
  if (type === "boolean") {
    return `<label class="pr-default-wrap"><span>${_msg("pub_param_default")}</span>
      <select class="pr-default-b"><option value="">—</option><option value="true"${p.default === true ? " selected" : ""}>true</option><option value="false"${p.default === false ? " selected" : ""}>false</option></select></label>`;
  }
  return `<div class="pr-inline">
    <label class="pr-ph-wrap"><span>placeholder</span><input class="pr-ph" maxlength="80" value="${_ltBack(p.placeholder).replace(/"/g, "&quot;")}"></label>
    <label><span>${_msg("pub_param_default")}</span><input class="pr-default-t" maxlength="200" value="${(p.default || "").replace(/"/g, "&quot;")}"></label>
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

function collectParams() {
  // 组装 params 数组; 校验失败返回 null 并行内标错
  let ok = true;
  const rows = [...document.querySelectorAll(".param-row")];
  rows.forEach(r => { const e = r.querySelector(".pub-err"); if (e) e.textContent = ""; });
  const seen = new Set();
  const out = [];
  for (const row of rows) {
    const id = row.dataset.pid;
    const key = row.querySelector(".pr-key").value.trim();
    const err = msg => { row.querySelector(".pub-err").textContent = _msg(msg); ok = false; };
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
  return ok ? out : null;
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
    // cur locale 三态 (批 2d):
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
  const body = {
    tool_id: document.getElementById("fToolId").value.trim(),
    label,
    desc: (desc || (!isEditPatch ? undefined : "")),
    url: document.getElementById("fUrl").value.trim(),
    timeout_s: parseInt(document.getElementById("fTimeout").value, 10) || 60,
    format_families: collectFamilies(),
    auth_header: document.getElementById("fAuthHeader").value.trim() || undefined,
    auth_token: document.getElementById("fAuthToken").value.trim() || undefined,
    cost: parseInt(document.getElementById("fCost").value, 10) || 0,
    params: params.length ? params : undefined,
  };
  if (!_editId) body.visibility = "private";   // 新工具恒私有起步; 编辑态不动可见性 (防公开工具被编辑静默降级)
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
    else location.href = "/account/tools?created=" + encodeURIComponent(body.tool_id);
    return;
  }
  // 服务端错误码 → 字段定位
  const map = {
    bad_url: "url", https_required: "url", url_taken: "url",
    bad_tool_id: "tool_id", tool_id_taken: "tool_id",
    bad_label: "label", bad_desc: "desc",
    bad_accept: "accept", bad_format_family: "accept",
    bad_output_filename: "output_filename", bad_output_content_type: "output_content_type",
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
  // 批 2c: 记录主框回填初值 (= resolver fallback) — 提交时值未变 → cur locale
  // 视为未修改, 不并入提交 dict (服务端 guard 的前端对应)
  _initLabel = document.getElementById("fLabel").value;
  _initDesc = document.getElementById("fDesc").value = _ltBack(t.desc);
  document.getElementById("fToolId").value = t.tool_id;
  document.getElementById("fToolId").readOnly = true;      // id immutable
  document.getElementById("fCost").value = t.cost != null ? t.cost : 0;
  document.getElementById("fTimeout").value = t.timeout_s || 60;
  renderFamGrid(famKeyFromAccept(t.accept));
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
  renderFamGrid([]);
  document.getElementById("addParam").addEventListener("click", () => addParamRow());
  document.getElementById("pubSubmit").addEventListener("click", submit);
  document.getElementById("pubCancel").addEventListener("click",
    () => { location.href = "/account/tools"; });
  if (_pubTool) refill(_pubTool);
  refreshParamNone();
}

init();
