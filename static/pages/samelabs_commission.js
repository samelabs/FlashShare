/* ── Samelabs commission setting  ─────────────────────
   POST /samelabs/economy/commission  {rate: 0..100 int}
   Two-step confirm on the money-adjacent write; toast feedback. */
(function () {
  var btn = document.getElementById('commissionSave');
  var input = document.getElementById('commissionInput');
  var msg = document.getElementById('commissionMsg');
  if (!btn || !input) return;
  function say(text, bad) {
    msg.textContent = text;
    msg.style.color = bad ? 'var(--danger, #b91c1c)' : 'var(--muted, #536471)';
  }
  btn.addEventListener('click', function () {
    var v = Number(input.value);
    if (!Number.isInteger(v) || v < 0 || v > 100) {
      say('只接受 0–100 的整数', true);
      return;
    }
    if (btn.dataset.armed !== '1') {
      btn.dataset.armed = '1';
      btn.classList.add('sl-btn-danger');
      btn.textContent = '确认保存 ' + v + '%？';
      setTimeout(function () {
        btn.dataset.armed = '';
        btn.classList.remove('sl-btn-danger');
        btn.textContent = '保存';
      }, 5000);
      return;
    }
    btn.dataset.armed = '';
    btn.disabled = true;
    slPost('/samelabs/economy/commission', { rate: v }).then(function (r) {
      btn.disabled = false;
      btn.classList.remove('sl-btn-danger');
      btn.textContent = '保存';
      if (r.data.ok) {
        say('已保存：' + r.data.rate + '%（已写审计）', false);
      } else {
        say('失败: ' + r.data.error, true);
      }
    });
  });
})();
