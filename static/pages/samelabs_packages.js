// 积分包: 创建 + 行内编辑 (名称/积分/价格) + 上下架
document.getElementById('pkgForm').addEventListener('submit', function (e) {
  e.preventDefault();
  var btn = document.getElementById('pkgCreate');
  var payload = {
    code: document.getElementById('pkgCode').value,
    name: document.getElementById('pkgName').value,
    credits: parseInt(document.getElementById('pkgCredits').value, 10) || 0,
    price: parseInt(document.getElementById('pkgPrice').value, 10) || 0
  };
  if (!payload.code || !payload.name || payload.credits <= 0) { slToast('err', '填写不完整', 'code / 名称 / 积分(≥1)为必填'); return; }
  slBusy(btn, slPost('/samelabs/packages', payload)).then(function (r) {
    if (r.ok && r.data.ok) {
      slToast('ok', '积分包已创建', r.data.package.code + ' · ' + r.data.package.name);
      setTimeout(function () { location.reload(); }, 600);
    } else {
      var why = r.data.error === 'code_taken' ? 'code 已存在' : (r.data.error || '请求失败');
      slToast('err', '创建失败', why);
    }
  });
});

document.querySelectorAll('.pkg-save').forEach(function (btn) {
  btn.addEventListener('click', function () {
    var tr = btn.closest('tr');
    var val = function (f) { return tr.querySelector('[data-field="' + f + '"]').value.trim(); };
    var payload = {
      name: val('name'),
      credits: parseInt(val('credits'), 10) || 0,
      price_cents: parseInt(val('price_cents'), 10) || 0
    };
    slBusy(btn, slPost('/samelabs/packages/' + btn.dataset.pkg, payload, 'PATCH')).then(function (r) {
      if (r.ok && r.data.ok) {
        slToast('ok', '已保存', r.data.package.code + ' · ' + (r.data.package.price_cents / 100).toFixed(2) + ' USD');
        setTimeout(function () { location.reload(); }, 600);
      } else {
        slToast('err', '保存失败', (r.data && r.data.error) || '请求失败');
      }
    });
  });
});

document.querySelectorAll('.pkg-toggle').forEach(function (btn) {
  btn.addEventListener('click', function () {
    var active = btn.dataset.active === '1';
    slBusy(btn, slPost('/samelabs/packages/' + btn.dataset.pkg, { active: !active }, 'PATCH')).then(function (r) {
      if (r.ok && r.data.ok) {
        location.reload();
      } else {
        slToast('err', active ? '下架失败' : '上架失败', (r.data && r.data.error) || '请求失败');
      }
    });
  });
});
