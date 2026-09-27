/* ── Samelabs payment review queue (B2b-1) ───────────────────
   resolve: POST /samelabs/economy/reviews/{id}/resolve
   Status flip only — the queue never moves money. */
(function () {
  function say(msg, ok) {
    if (typeof slToast === 'function') return slToast(ok === false ? 'err' : 'ok', msg);
    var t = document.getElementById('slToast');
    if (t) { t.textContent = msg; t.hidden = false; setTimeout(function () { t.hidden = true; }, 2200); }
  }
  function reload() { setTimeout(function () { location.reload(); }, 700); }

  document.querySelectorAll('.btn-resolve').forEach(function (b) {
    var armed = false, t = 0;
    b.addEventListener('click', function () {
      if (!armed) {
        armed = true; b.classList.add('sl-btn-danger'); b.textContent = '确认已处理';
        t = setTimeout(function () {
          armed = false; b.classList.remove('sl-btn-danger'); b.textContent = 'Resolve';
        }, 5000);
        return;
      }
      clearTimeout(t); b.disabled = true;
      slPost('/samelabs/economy/reviews/' + b.dataset.rid + '/resolve', {})
        .then(function (r) {
          if (r.data.ok) { say('已标记处理完成（仅状态 + 审计）', true); reload(); }
          else { say('失败: ' + r.data.error, false); b.disabled = false; armed = false; }
        });
    });
  });
})();
