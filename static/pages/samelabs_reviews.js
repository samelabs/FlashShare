/* ── Samelabs 支付审核队列 ────────────────────────────────────
   标记已处理: POST /samelabs/economy/reviews/{id}/resolve
   仅状态翻转 + 审计，队列本身不改动任何余额。 */
(function () {
  function say(msg, ok) {
    if (typeof slToast === 'function') return slToast(ok === false ? 'err' : 'ok', msg);
    var t = document.getElementById('slToast');
    if (t) { t.textContent = msg; t.hidden = false; setTimeout(function () { t.hidden = true; }, 2200); }
  }
  function reload() { setTimeout(function () { location.reload(); }, 700); }

  document.querySelectorAll('.js-resolve').forEach(function (b) {
    var armed = false, t = 0;
    b.addEventListener('click', function () {
      if (!armed) {
        armed = true; b.classList.add('sl-btn-danger'); b.textContent = '确认已处理？';
        t = setTimeout(function () {
          armed = false; b.classList.remove('sl-btn-danger'); b.textContent = '标记已处理';
        }, 5000);
        return;
      }
      clearTimeout(t); b.disabled = true;
      slPost('/samelabs/economy/reviews/' + b.dataset.rid + '/resolve', {})
        .then(function (r) {
          if (r.data.ok) { say('已标记处理完成', true); reload(); }
          else { say('失败: ' + r.data.error, false); b.disabled = false; armed = false; }
        });
    });
  });
})();
