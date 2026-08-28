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
