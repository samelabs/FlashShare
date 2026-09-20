// ── shared page helpers (single source; every pages/*.js may use) ──
window._msg = k => { const v = window.t(k); return (v && v !== k) ? v : k; };
window._errm = code => {
  if (!code) return window.t("failed");
  code = String(code);
  // Platform i18n layer (protocol error codes are _msg keys, NOT LocalizedText —
  // user-declared text resolves via resolveLabel, never through this path).
  if (window._msg(code) !== code) return window._msg(code);
  if (code.startsWith('err_')) return code;
  const k = 'mytools_err_' + code;
  if (window._msg(k) !== k) return window._msg(k);
  const k2 = 'err_' + code;
  if (window._msg(k2) !== k2) return window._msg(k2);
  // Unknown machine code (e.g. tool_failed with no i18n key) — fall back
  // to the canonical user-facing "failed" text, never raw code.
  return window.t("failed");
};
