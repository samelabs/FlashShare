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


// 行内保存: 价格 + Creem product id
document.querySelectorAll(".plan-save").forEach(function (btn) {
  btn.addEventListener("click", function () {
    var tr = btn.closest("tr");
    var planId = btn.dataset.plan;
    var price = parseInt(tr.querySelector('[data-field="price_cents"]').value, 10) || 0;
    var creemId = tr.querySelector('[data-field="creem_price_id"]').value.trim();
    slBusy(btn, slPost("/samelabs/plans/" + planId, { price_cents: price, creem_price_id: creemId }, "PATCH")).then(function (r) {
      if (r.ok && r.data.ok) {
        slToast("ok", "已保存", r.data.plan.code + " · " + (creemId || "未绑价"));
      } else {
        slToast("err", "保存失败", (r.data && r.data.error) || "请求失败");
      }
    });
  });
});
