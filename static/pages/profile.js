document.getElementById('unbindX')?.addEventListener('click', async () => {
  if (!confirm('window.t("confirmUnbindX")')) return;
  const r = await fetch('/auth/x/unbind', { method: 'POST' });
  if (r.ok) location.reload();
  else { const d = await r.json(); alert(d.error || 'error'); }
});
