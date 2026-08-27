async function _copyText(btn, text) {
  try { await navigator.clipboard.writeText(text); } catch (e) {}
  const old = btn.innerHTML;
  btn.innerHTML = "✓ 已复制";
  btn.classList.add("copied");
  setTimeout(() => { btn.innerHTML = old; btn.classList.remove("copied"); }, 1500);
}
for (const [id, btn] of [["mcpUrl","copyMcp"],["mcpConf","copyConf"]]) {
  const el = document.getElementById(btn);
  if (!el) continue;
  el.addEventListener("click", () => _copyText(el, document.getElementById(id).textContent));
}
