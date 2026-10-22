/* ── Samelabs 共享层: toast + fetch helper ─────────────────
   所有页面操作反馈统一走 toast; alert 一律禁止。 */
var SL_ICO_OK  = '<svg class="sl-toast-ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/><polyline points="22 4 12 14.01 9 11.01"/></svg>';
var SL_ICO_ERR = '<svg class="sl-toast-ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><line x1="15" y1="9" x2="9" y2="15"/><line x1="9" y1="9" x2="15" y2="15"/></svg>';
var SL_ICO_X   = window.iconSvg("close");

function slToast(kind, title, sub) {
  var zone = document.getElementById('slToasts');
  var el = document.createElement('div');
  el.className = 'sl-toast ' + kind;
  el.innerHTML = (kind === 'ok' ? SL_ICO_OK : SL_ICO_ERR) +
    '<div class="sl-toast-body"><strong></strong>' + (sub ? '<span></span>' : '') + '</div>' +
    '<button class="sl-toast-x" type="button" aria-label="关闭">' + SL_ICO_X + '</button>';
  el.querySelector('strong').textContent = title;
  if (sub) el.querySelector('span').textContent = sub;
  zone.appendChild(el);
  var kill = function () {
    if (el.classList.contains('leaving')) return;
    el.classList.add('leaving');
    setTimeout(function () { el.remove(); }, 220);
  };
  el.querySelector('.sl-toast-x').addEventListener('click', kill);
  setTimeout(kill, 3500);
}

/* POST JSON, 返回 {status, data} — 调用方负责业务分支 */
function slPost(url, payload, method) {
  return fetch(url, {
    method: method || 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload)
  }).then(function (r) { return r.json().then(function (d) { return { ok: r.ok, data: d }; }); })
    .catch(function () { return { ok: false, data: { error: 'network' } }; });
}

var slFmt = function (n) { return Number(n).toLocaleString('en-US'); };

/* 按钮 loading 守卫: 请求期间加 loading + disabled, 结束移除 */
function slBusy(btn, promise) {
  btn.classList.add('loading');
  btn.disabled = true;
  return promise.finally(function () {
    btn.classList.remove('loading');
    btn.disabled = false;
  });
}
