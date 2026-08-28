document.getElementById('planForm').addEventListener('submit', function (e) {
  e.preventDefault();
  var btn = document.getElementById('planCreate');
  var payload = {
    code: document.getElementById('planCode').value,
    name: document.getElementById('planName').value,
    monthly_price: parseInt(document.getElementById('planPrice').value, 10) || 0,
    monthly_credits: parseInt(document.getElementById('planCredits').value, 10) || 0
  };
  if (!payload.code || !payload.name) { slToast('err', '填写不完整', 'code 与名称为必填'); return; }
  slBusy(btn, slPost('/samelabs/plans', payload)).then(function (r) {
    if (r.ok && r.data.ok) {
      slToast('ok', '档位已创建', r.data.plan.code + ' · ' + r.data.plan.name);
      setTimeout(function () { location.reload(); }, 600);
    } else {
      var why = r.data.error === 'code_taken' ? 'code 已存在' : (r.data.error || '请求失败');
      slToast('err', '创建失败', why);
    }
  });
});
