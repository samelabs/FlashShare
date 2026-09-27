/* ── Samelabs 提现队列 ────────────────────────────────────────
   批准      POST /samelabs/economy/withdrawals/{id}/approve   (待审→已批准)
   驳回      POST /samelabs/economy/withdrawals/{id}/reject    (待审→已驳回)
   已打款    POST /samelabs/economy/withdrawals/{id}/paid      (已批准→已打款，需打款凭证)
   打款失败  POST /samelabs/economy/withdrawals/{id}/payout-failed (回退收益池)
   资金操作一律两步确认 + toast 反馈。 */
(function () {
  function say(msg, ok) {
    if (typeof slToast === 'function') return slToast(ok === false ? 'err' : 'ok', msg);
    var t = document.getElementById('slToast');
    if (t) { t.textContent = msg; t.hidden = false; setTimeout(function () { t.hidden = true; }, 2200); }
  }
  function reload() { setTimeout(function () { location.reload(); }, 700); }

  document.querySelectorAll('.js-approve').forEach(function (b) {
    var armed = false, t = 0;
    b.addEventListener('click', function () {
      if (!armed) {
        armed = true; b.classList.add('btn sl-btn-danger'); b.textContent = '确认批准？';
        t = setTimeout(function () { armed = false; b.classList.remove('btn sl-btn-danger'); b.textContent = '批准'; }, 5000);
        return;
      }
      clearTimeout(t); b.disabled = true;
      slPost('/samelabs/economy/withdrawals/' + b.dataset.wid + '/approve', {})
        .then(function (r) {
          if (r.data.ok) { say('已批准并扣减收益池', true); reload(); }
          else { say('失败: ' + r.data.error, false); b.disabled = false; armed = false; b.textContent = '批准'; }
        });
    });
  });

  document.querySelectorAll('.js-reject').forEach(function (b) {
    var armed = false, t = 0;
    b.addEventListener('click', function () {
      if (!armed) {
        armed = true; b.textContent = '确认驳回？'; b.classList.add('sl-btn-danger');
        t = setTimeout(function () { armed = false; b.textContent = '驳回'; b.classList.remove('sl-btn-danger'); }, 3000);
        return;
      }
      clearTimeout(t); b.disabled = true;
      slPost('/samelabs/economy/withdrawals/' + b.dataset.wid + '/reject', {})
        .then(function (r) {
          if (r.data.ok) { say('已驳回（不扣款）', true); reload(); }
          else { say('失败: ' + r.data.error, false); b.disabled = false; }
        });
    });
  });

  document.querySelectorAll('.js-payout-failed').forEach(function (b) {
    var armed = false, t = 0, reasonInput = null;
    b.addEventListener('click', function () {
      var tr = b.closest('tr');
      if (!armed) {
        armed = true; b.textContent = '填原因并确认';
        b.classList.add('sl-btn-danger');
        reasonInput = document.createElement('input');
        reasonInput.className = 'sl-input js-payout-reason-input';
        reasonInput.placeholder = '打款失败原因（必填）';
        reasonInput.style.cssText = 'width:140px;padding:4px 8px;font-size:13px';
        tr.querySelector('td:nth-child(10)').appendChild(reasonInput);
        reasonInput.focus();
        t = setTimeout(function () {
          armed = false; b.textContent = '打款失败…';
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
            b.disabled = false; armed = false; b.textContent = '打款失败…';
          }
        });
    });
  });

  document.querySelectorAll('.js-paid').forEach(function (b) {
    var armed = false, t = 0, refInput = null;
    b.addEventListener('click', function () {
      var tr = b.closest('tr');
      if (!armed) {
        armed = true; b.textContent = '填凭证并确认';
        refInput = document.createElement('input');
        refInput.className = 'sl-input js-payout-ref-input';
        refInput.placeholder = '打款凭证（必填）';
        refInput.style.cssText = 'width:140px;padding:4px 8px;font-size:13px';
        tr.querySelector('td:nth-child(8)').appendChild(refInput);
        refInput.focus();
        t = setTimeout(function () { armed = false; b.textContent = '标记已打款…'; if (refInput) refInput.remove(); }, 20000);
        return;
      }
      var ref = refInput ? refInput.value.trim() : '';
      if (!ref) { say('打款凭证必填', false); refInput.focus(); return; }
      clearTimeout(t); b.disabled = true;
      slPost('/samelabs/economy/withdrawals/' + b.dataset.wid + '/paid', { payout_ref: ref })
        .then(function (r) {
          if (r.data.ok) { say('已标记打款', true); reload(); }
          else {
            say('失败: ' + r.data.error, false);
            b.disabled = false; armed = false; b.textContent = '标记已打款…';
          }
        });
    });
  });
})();
