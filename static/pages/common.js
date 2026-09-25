// ── shared page helpers (single source; every pages/*.js may use) ──
window._msg = k => { const v = window.t(k); return (v && v !== k) ? v : k; };
// Unified user-visible error boundary (#42):
//   known code → specific localized message
//   unknown err_* / machine code / any non-code diagnostic → the
//   canonical localized generic failure (err_unexpected)
// A raw err_* key, 'error' or exception text is never returned.
window._errm = code => {
  if (code === null || code === undefined || code === "") return window.t("err_unexpected");
  code = String(code);
  // Platform i18n layer (protocol error codes are _msg keys, NOT LocalizedText —
  // user-declared text resolves via resolveLabel, never through this path).
  if (window._msg(code) !== code) return window._msg(code);
  const k = 'mytools_err_' + code;
  if (window._msg(k) !== k) return window._msg(k);
  const k2 = 'err_' + code;
  if (window._msg(k2) !== k2) return window._msg(k2);
  // unknown machine code — canonical localized generic failure, never raw
  return window.t("err_unexpected");
};
