// Profile page: X unbind flows through the site-wide async confirm
// authority (app.js window.confirmAction) — never native confirm(), and
// never raw i18n source in the dialog text.
document.getElementById('unbindX')?.addEventListener('click', async () => {
  const ok = await window.confirmAction({
    title: window.t("xUnbind"),
    message: window.t("confirmUnbindX"),
    confirmLabel: window.t("confirm"),
    cancelLabel: window.t("cancel"),
  });
  if (!ok) return;
  const r = await fetch('/auth/x/unbind', { method: 'POST' });
  if (r.ok) location.reload();
  else {
    const d = await r.json().catch(() => ({}));
    alert(_errm(d.error));
  }
});
