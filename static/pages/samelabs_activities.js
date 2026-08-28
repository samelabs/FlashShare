function saveAct(code, payload) {
  return slPost('/samelabs/activities/' + code + '/config', payload);
}

document.querySelectorAll('.btn-save-act').forEach(function (btn) {
  btn.addEventListener('click', function () {
    var code = btn.dataset.act;
    var input = document.querySelector('.act-amount[data-act="' + code + '"]');
    var amount = parseInt(input.value, 10);
    if (isNaN(amount) || amount < 0) {
      slToast('err', '额度无效', code + ' · 需为 0 – 1000000 的整数');
      input.focus();
      return;
    }
    slBusy(btn, saveAct(code, { amount: amount })).then(function (r) {
      if (r.ok && r.data.ok) {
        slToast('ok', '额度已保存', code + ' = ' + r.data.activity.amount + ' 积分');
      } else {
        slToast('err', '保存失败', code + ' · ' + (r.data.error || '请求失败'));
      }
    });
  });
});

document.querySelectorAll('.btn-toggle-act').forEach(function (btn) {
  btn.addEventListener('click', function () {
    var code = btn.dataset.act;
    var nowEnabled = btn.dataset.enabled === 'true';
    slBusy(btn, saveAct(code, { active: !nowEnabled })).then(function (r) {
      if (r.ok && r.data.ok) {
        var row = document.querySelector('tr[data-act="' + code + '"]');
        var badge = row.querySelector('.act-active .sl-badge');
        var on = r.data.activity.active;
        badge.className = 'sl-badge ' + (on ? 'sl-b-ok' : 'sl-b-muted');
        badge.textContent = on ? '启用' : '停用';
        btn.dataset.enabled = on ? 'true' : 'false';
        btn.textContent = on ? '停用' : '启用';
        slToast('ok', on ? '活动已启用' : '活动已停用', code);
      } else {
        slToast('err', '操作失败', code + ' · ' + (r.data.error || '请求失败'));
      }
    });
  });
});

/* 窗口编辑: 行内替换为两个 datetime 输入 + 保存/清除 */
document.querySelectorAll('.btn-window-act').forEach(function (btn) {
  btn.addEventListener('click', function () {
    var code = btn.dataset.act;
    var cell = document.querySelector('.act-window[data-act="' + code + '"]');
    if (cell.querySelector('input')) return;  // already editing
    var row = cell.closest('tr');
    var ds = row.dataset.starts || '', de = row.dataset.ends || '';
    function toLocal(v) { return v ? new Date(v * 1000).toISOString().slice(0, 16) : ''; }
    cell.innerHTML = '<input type="datetime-local" class="sl-adj-in w-start" value="' + toLocal(ds) + '" style="width:150px"> '
                   + '<input type="datetime-local" class="sl-adj-in w-end" value="' + toLocal(de) + '" style="width:150px">';
    var save = document.createElement('button');
    save.type = 'button'; save.className = 'sl-btn sl-btn-ghost sl-btn-sm'; save.textContent = '存';
    save.addEventListener('click', function () {
      var s = cell.querySelector('.w-start').value;
      var e = cell.querySelector('.w-end').value;
      var payload = {
        starts_at: s ? Math.floor(new Date(s + 'Z').getTime() / 1000) : 0,
        ends_at: e ? Math.floor(new Date(e + 'Z').getTime() / 1000) : 0,
      };
      slBusy(save, saveAct(code, payload)).then(function (r) {
        if (r.ok && r.data.ok) { location.reload(); }
        else { slToast('err', '窗口保存失败', r.data.error || '请求失败'); }
      });
    });
    cell.appendChild(save);
  });
});
