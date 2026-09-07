// ── Publish page (/account/tools/publish) — standalone form: create + edit.
// 职责: 表单状态/参数行编辑器/族勾选/校验/提交。列表逻辑归 tools.js, 不共享。
"use strict";

const _editId = new URLSearchParams(location.search).get("id") || "";
const _pubTool = window.__PUB_TOOL__ || null;

// ── helpers (single source: common.js _msg/_errm) ──
const _curLang = () => (document.documentElement.lang || "en").slice(0, 2);

// ── 场景格式族勾选器 (词表镜像 governance/formats.py; 详见 formats.py) ──
const EXT_BY_MIME = {
  "image/jpeg": "image", "image/png": "image", "image/gif": "image", "image/webp": "image",
  "image/bmp": "image", "image/tiff": "image", "image/heic": "image", "image/avif": "image",
  "image/x-icon": "image", "image/vnd.microsoft.icon": "image", "image/svg+xml": "image",
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
// ── 场景格式族 (0930 范式 v2): 输入族**单选必选** — 词表镜像 governance/formats.py ──
const FAM_KEYS = ["image", "document", "archive", "cad3d", "media", "data", "code"];
const FAM_I18N = {image: "family_image", document: "family_document",
                  archive: "family_archive", cad3d: "family_cad3d",
                  media: "family_media", data: "family_data", code: "family_code"};
const FAM_DETAIL = {
  image: "JPG PNG GIF WebP BMP HEIC HEIF TIFF AVIF ICO SVG",
  document: "PDF TXT CSV MD RTF DOC DOCX XLS XLSX PPT PPTX ODT ODS ODP",
  archive: "ZIP RAR 7Z GZ TAR BZ2 XZ TGZ",
  cad3d: "DWG DXF STEP IGES STL OBJ FBX GLB GLTF 3DS SKP BLEND SLDPRT IAM CATPART",
  media: "MP3 WAV FLAC AAC OGG M4A MP4 MOV WEBM AVI MKV",
  data: "JSON XML YAML YML",
  code: "PY JS TS HTML CSS SQL",
};
function renderFamGrid(preselected) {
  const grid = document.getElementById("famGrid");
  const sel = preselected && preselected.length ? preselected[0] : FAM_KEYS[0]; // 默认 image, 不可空
  grid.innerHTML = FAM_KEYS.map(k =>
    '<label class="fam-chip" title="' + FAM_DETAIL[k] + '"><input type="radio" name="fam" value="' + k + '"' +
    (k === sel ? " checked" : "") + ">" +
    "<span>" + _msg(FAM_I18N[k]) + "</span></label>").join("");
  // 摘要行: 单行, 随选中族实时更新 (明细的常驻清单是文档排版, 不进表单;
  // 完整明细在 chip title 悬停)
  updateFamSummary();
  grid.addEventListener("change", updateFamSummary);
}
function updateFamSummary() {
  const sel = document.querySelector("#famGrid input:checked");
  const key = sel ? sel.value : FAM_KEYS[0];
  const hint = document.getElementById("famHint");
  hint.textContent = _msg(FAM_I18N[key]) + ": " + FAM_DETAIL[key];
}
function collectFamilies() {
  const sel = document.querySelector("#famGrid input:checked");
  return sel ? sel.value : FAM_KEYS[0];   // 单选必选: 无空态
}
function famKeyFromMimes(mimes) {
  const fams = new Set();
  (mimes || []).forEach(m => { const f = EXT_BY_MIME[m]; if (f) fams.add(f); });
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
    if (lang && name) out[lang] = name;
    if (lang && desc) out[lang + "__desc"] = desc;
  });
  return out;
}

// ── 参数行编辑器 (四型: enum/text/number/boolean; 服务端镜像校验) ──
const PARAM_TYPES = [
  ["enum", "pub_pt_enum"], ["text", "pub_pt_text"],
  ["number", "pub_pt_number"], ["boolean", "pub_pt_boolean"],
];
const KEY_RE = /^[a-z][a-z0-9_]{0,31}$/;
let _paramSeq = 0;

function paramRowHtml(p) {
  const id = "pr" + (++_paramSeq);
  const typeOpts = PARAM_TYPES.map(([v, k]) =>
    `<option value="${v}"${p.type === v ? " selected" : ""}>${_msg(k)}</option>`).join("");
  return `<div class="param-row" data-pid="${id}">
    <div class="param-row-head">
      <input class="pr-key" placeholder="style" maxlength="32" value="${(p.key || "").replace(/"/g, "&quot;")}">
      <select class="pr-type">${typeOpts}</select>
      <input class="pr-label" maxlength="80" placeholder="${_msg("pub_param_label_ph")}" value="${typeof p.label === "string" ? p.label.replace(/"/g, "&quot;") : ""}">
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
      `<input class="pr-opt-label" maxlength="40" placeholder="${_msg("pub_param_optlabel_ph")}" value="${typeof o.label === "string" ? o.label.replace(/"/g, "&quot;") : (typeof o.label === "object" ? Object.values(o.label)[0] || "" : "")}">` +
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
    <label class="pr-ph-wrap"><span>placeholder</span><input class="pr-ph" maxlength="80" value="${(p.placeholder || "").replace(/"/g, "&quot;")}"></label>
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
    if (label) labelDict[_curLang()] = label;
    if (desc) descDict[_curLang()] = desc;
    if (Object.keys(labelDict).length > 1 || Object.keys(descDict).length) {
      label = Object.keys(labelDict).length ? labelDict : label;
      desc = Object.keys(descDict).length ? descDict : desc;
    }
  }
  const body = {
    tool_id: document.getElementById("fToolId").value.trim(),
    label, desc: desc || undefined,
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
    headers: { "Content-Type": "application/json" },
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
  document.getElementById("fUrl").value = t.url || "";
  document.getElementById("fLabel").value = typeof t.label === "string" ? t.label
    : (t.label[_curLang()] || t.label.en || Object.values(t.label)[0] || "");
  document.getElementById("fToolId").value = t.tool_id;
  document.getElementById("fToolId").readOnly = true;      // id immutable
  document.getElementById("fDesc").value = typeof t.desc !== "object" || !t.desc
    ? (t.desc || "") : (t.desc[_curLang()] || t.desc.en || Object.values(t.desc)[0] || "");
  document.getElementById("fCost").value = t.cost != null ? t.cost : 0;
  document.getElementById("fTimeout").value = t.timeout_s || 60;
  renderFamGrid(famKeyFromMimes(t.accept));
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
  document.querySelector("#langRows .lang-row .lang-del")?.addEventListener("click",
    e => e.target.closest(".lang-row").remove(), { once: true });
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
    box.lastElementChild.querySelector(".lang-del").addEventListener("click",
      e => e.target.closest(".lang-row").remove());
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
