/* ── 积分调整两态交互 ──
   静默态: ±N 输入 + ghost [调整]
   确认态: 合法非零 → 按钮变 "+N 确认"(primary) / "-N 确认"(danger), 输入框染语义色
   确认 → loading → POST → toast + 余额行内更新 (无 reload) */
document.querySelectorAll('.sl-adj').forEach(function (adj) {
  var uid = adj.dataset.uid;
  var input = adj.querySelector('.sl-adj-in');
  var btn = adj.querySelector('.adj-go');
  var balEl = document.querySelector('tr[data-uid="' + uid + '"] .u-balance');
  var userEl = adj.closest('tr').querySelector('.sl-u-names strong');
  var user = userEl ? userEl.textContent : '#' + uid;

  function reset() {
    input.value = '';
    input.classList.remove('plus', 'minus');
    btn.className = 'sl-btn sl-btn-ghost sl-btn-sm adj-go';
    btn.textContent = '调整';
  }

  input.addEventListener('input', function () {
    var v = parseInt(input.value, 10);
    if (isNaN(v) || v === 0) { reset(); return; }
    input.classList.toggle('plus', v > 0);
    input.classList.toggle('minus', v < 0);
    if (v > 0) { btn.className = 'sl-btn sl-btn-primary sl-btn-sm adj-go'; btn.textContent = '+' + v + ' 确认'; }
    else { btn.className = 'sl-btn sl-btn-danger sl-btn-sm adj-go'; btn.textContent = v + ' 确认'; }
  });

  input.addEventListener('keydown', function (e) {
    if (e.key === 'Enter') { e.preventDefault(); btn.click(); }
    if (e.key === 'Escape') reset();
  });

  btn.addEventListener('click', function () {
    var v = parseInt(input.value, 10);
    if (isNaN(v) || v === 0) {
      input.focus();
      input.style.borderColor = 'var(--sl-err)';
      setTimeout(function () { input.style.borderColor = ''; }, 600);
      return;
    }
    var before = parseInt(balEl.textContent.replace(/,/g, ''), 10);
    slBusy(btn, slPost('/samelabs/users/' + uid + '/credits', { amount: v })).then(function (r) {
      if (r.ok && r.data.ok) {
        balEl.textContent = slFmt(r.data.balance_after);
        slToast('ok', '已' + (v > 0 ? '发放' : '扣除') + ' ' + Math.abs(v) + ' 积分',
                user + ': ' + slFmt(before) + ' → ' + slFmt(r.data.balance_after));
        reset();
      } else {
        var why = r.data && r.data.error ? r.data.error : '请求失败';
        slToast('err', '调整失败', user + ' · ' + why);
      }
    });
  });
});
