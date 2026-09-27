/* ── Samelabs withdrawal queue (S4 lifecycle) ─────────────────
   approve  POST /samelabs/economy/withdrawals/{id}/approve   (pending→approved)
   reject   POST /samelabs/economy/withdrawals/{id}/reject    (pending→rejected)
   paid     POST /samelabs/economy/withdrawals/{id}/paid      (approved→paid, payout_ref required)
   Two-state confirm on every financial action; toast feedback. */
(function () {
  function say(msg, ok) {
    if (typeof slToast === 'function') return slToast(ok === false ? 'err' : 'ok', msg);
    var t = document.getElementById('slToast');
    if (t) { t.textContent = msg; t.hidden = false; setTimeout(function () { t.hidden = true; }, 2200); }
  }
  function reload() { setTimeout(function () { location.reload(); }, 700); }

  document.querySelectorAll('.btn-approve').forEach(function (b) {
    var armed = false, t = 0;
    b.addEventListener('click', function () {
      if (!armed) {
        armed = true; b.classList.add('btn sl-btn-danger'); b.textContent = '确认批准';
        t = setTimeout(function () { armed = false; b.classList.remove('btn sl-btn-danger'); b.textContent = 'Approve'; }, 5000);
        return;
      }
      clearTimeout(t); b.disabled = true;
      slPost('/samelabs/economy/withdrawals/' + b.dataset.wid + '/approve', {})
        .then(function (r) {
          if (r.data.ok) { say('已批准并记账 (1 withdraw ledger row)', true); reload(); }
          else { say('失败: ' + r.data.error, false); b.disabled = false; armed = false; b.textContent = 'Approve'; }
        });
    });
  });

  document.querySelectorAll('.btn-reject').forEach(function (b) {
    var armed = false, t = 0;
    b.addEventListener('click', function () {
      if (!armed) {
        armed = true; b.textContent = 'btn 确认驳回?'; b.classList.add('sl-btn-danger');
        t = setTimeout(function () { armed = false; b.textContent = 'btn Reject'; b.classList.remove('sl-btn-danger'); }, 3000);
        return;
      }
      clearTimeout(t); b.disabled = true;
      slPost('/samelabs/economy/withdrawals/' + b.dataset.wid + '/reject', {})
        .then(function (r) {
          if (r.data.ok) { say('已驳回 (零记账)', true); reload(); }
          else { say('失败: ' + r.data.error, false); b.disabled = false; }
        });
    });
  });

  document.querySelectorAll('.btn-payout-failed').forEach(function (b) {
    var armed = false, t = 0, reasonInput = null;
    b.addEventListener('click', function () {
      var tr = b.closest('tr');
      if (!armed) {
        armed = true; b.textContent = '填原因并确认';
        b.classList.add('sl-btn-danger');
        reasonInput = document.createElement('input');
        reasonInput.className = 'sl-input payout-reason-input';
        reasonInput.placeholder = '失败原因 (必填)';
        reasonInput.style.cssText = 'width:140px;padding:4px 8px;font-size:13px';
        tr.querySelector('td:nth-child(10)').appendChild(reasonInput);
        reasonInput.focus();
        t = setTimeout(function () {
          armed = false; b.textContent = 'Payout failed…';
          b.classList.remove('sl-btn-danger');
          if (reasonInput) reasonInput.remove();
        }, 20000);
        return;
      }
      var reason = reasonInput ? reasonInput.value.trim() : '';
      if (!reason) { say('失败原因必填', false); reasonInput.focus(); return; }
      clearTimeout(t); b.disabled = true;
      slPost('/samelabs/economy/withdrawals/' + b.dataset.wid + '/payout-failed', { reason: reason })
        .then(function (r) {
          if (r.data.ok) { say('已标记打款失败，金额已退回收益池', true); reload(); }
          else {
            say('失败: ' + r.data.error, false);
            b.disabled = false; armed = false; b.textContent = 'Payout failed…';
          }
        });
    });
  });

  document.querySelectorAll('.btn-paid').forEach(function (b) {
    var armed = false, t = 0, refInput = null;
    b.addEventListener('click', function () {
      var tr = b.closest('tr');
      if (!armed) {
        armed = true; b.textContent = '填凭证并确认';
        refInput = document.createElement('input');
        refInput.className = 'sl-input payout-ref-input';
        refInput.placeholder = 'payout_ref (必填)';
        refInput.style.cssText = 'width:140px;padding:4px 8px;font-size:13px';
        tr.querySelector('td:nth-child(8)').appendChild(refInput);
        refInput.focus();
        t = setTimeout(function () { armed = false; b.textContent = 'Mark paid…'; if (refInput) refInput.remove(); }, 20000);
        return;
      }
      var ref = refInput ? refInput.value.trim() : '';
      if (!ref) { say('payout_ref 必填', false); refInput.focus(); return; }
      clearTimeout(t); b.disabled = true;
      slPost('/samelabs/economy/withdrawals/' + b.dataset.wid + '/paid', { payout_ref: ref })
        .then(function (r) {
          if (r.data.ok) { say('已标记打款 (零新记账)', true); reload(); }
          else {
            say('失败: ' + r.data.error, false);
            b.disabled = false; armed = false; b.textContent = 'Mark paid…';
          }
        });
    });
  });
})();
