// ── shared page helpers (single source; every pages/*.js may use) ──
window._msg = k => { const v = window.t(k); return (v && v !== k) ? v : k; };
window._errm = code => {
  if (!code) return 'failed';
  if (String(code).startsWith('err_')) return window._msg(code) !== code ? window._msg(code) : code;
  const k = 'mytools_err_' + code;
  if (window._msg(k) !== k) return window._msg(k);
  const k2 = 'err_' + code;
  return window._msg(k2) !== k2 ? window._msg(k2) : code;
};
