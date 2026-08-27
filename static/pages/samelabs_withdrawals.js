(function () {
  var toast = document.getElementById('slToast');
  function say(msg) { toast.textContent = msg; toast.hidden = false; setTimeout(function () { toast.hidden = true; }, 2200); }
  function reload() { setTimeout(function () { location.reload(); }, 600); }

  document.querySelectorAll('.btn-approve').forEach(function (b) {
    var armed = false, t = 0;
    b.addEventListener('click', function () {
      var tr = b.closest('tr');
      var refInput = tr.querySelector('.txn-input');
      if (!armed) {  // two-state: first click reveals the txn input, second submits
        armed = true; b.classList.add('danger'); b.textContent = '确认批准';
        if (!refInput) {
          refInput = document.createElement('input');
          refInput.className = 'sl-input txn-input';
          refInput.placeholder = '打款凭证号(可选)';
          refInput.style.cssText = 'width:130px;padding:4px 8px;font-size:13px';
          tr.querySelector('td:nth-child(6)').appendChild(refInput);
        }
        refInput.focus();
        t = setTimeout(function () { armed = false; b.classList.remove('danger'); b.textContent = '批准'; }, 5000);
        return;
      }
      clearTimeout(t); b.disabled = true;
      fetch('/samelabs/withdrawals/' + b.dataset.wid + '/process', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ approve: true, txn_ref: (refInput ? refInput.value.trim() : '') })
      }).then(function (r) { return r.json(); }).then(function (d) {
        if (d.ok) { say('已批准并记账'); reload(); } else { say('失败: ' + d.error); b.disabled = false; armed = false; b.textContent = '批准'; }
      });
    });
  });
  document.querySelectorAll('.btn-reject').forEach(function (b) {
    var armed = false, t = 0;
    b.addEventListener('click', function () {
      if (!armed) {  // two-state (site pattern: no confirm())
        armed = true; b.textContent = '确认驳回?'; b.classList.add('sl-btn-danger');
        t = setTimeout(function () { armed = false; b.textContent = '驳回'; b.classList.remove('sl-btn-danger'); }, 3000);
        return;
      }
      clearTimeout(t);
      fetch('/samelabs/withdrawals/' + b.dataset.wid + '/process', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ approve: false })
      }).then(function (r) { return r.json(); }).then(function (d) {
        if (d.ok) { say('已驳回'); reload(); } else say('失败: ' + d.error);
      });
    });
  });
})();
