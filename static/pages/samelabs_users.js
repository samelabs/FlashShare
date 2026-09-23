/* ── Users: credits adjustment modal (stable action_id idempotency) ── */
(function () {
  var modal = document.getElementById('adjustModal');
  if (!modal) return;
  var form = document.getElementById('adjustForm');
  var nameEl = document.getElementById('adjustName');
  var currentUid = null;
  var actionId = null;

  document.querySelectorAll('[data-adjust]').forEach(function (b) {
    b.addEventListener('click', function () {
      currentUid = b.dataset.adjust;
      nameEl.textContent = b.dataset.username + ' (#' + currentUid + ')';
      form.amount.value = ''; form.note.value = '';
      actionId = null;
      if (typeof modal.showModal === 'function') modal.showModal();
      else modal.setAttribute('open', '');
      form.amount.focus();
    });
  });

  document.getElementById('adjustCancel').addEventListener('click', function () {
    if (typeof modal.close === 'function') modal.close(); else modal.removeAttribute('open');
  });

  document.getElementById('adjustSubmit').addEventListener('click', function () {
    var btn = this;
    var v = parseInt(form.amount.value, 10);
    if (isNaN(v) || v === 0) { form.amount.focus(); return; }
    if (!actionId) {
      actionId = Date.now().toString(36) + '-' + Math.random().toString(36).slice(2, 10);
      form.action_id.value = actionId;
    }
    slBusy(btn, slPost('/samelabs/users/' + currentUid + '/credits',
                       { amount: v, note: form.note.value, action_id: actionId }))
      .then(function (r) {
        if (r.ok && r.data.ok) {
          slToast('ok', '已' + (v > 0 ? '发放' : '扣除') + ' ' + Math.abs(v) + ' Credits',
                  '新余额 ' + slFmt(r.data.balance_after));
          actionId = null;
          setTimeout(function () { location.reload(); }, 700);
        } else {
          slToast('err', '调整失败', (r.data && r.data.error) || '请求失败');
        }
      });
  });
})();
