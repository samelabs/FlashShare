// 行内保存: 价格 + Creem product id
document.querySelectorAll(".plan-save").forEach(function (btn) {
  btn.addEventListener("click", function () {
    var tr = btn.closest("tr");
    var planId = btn.dataset.plan;
    var price = parseInt(tr.querySelector('[data-field="price_cents"]').value, 10) || 0;
    var creemId = tr.querySelector('[data-field="creem_product_id"]').value.trim();
    slBusy(btn, slPost("/samelabs/plans/" + planId, { price_cents: price, creem_product_id: creemId }, "PATCH")).then(function (r) {
      if (r.ok && r.data.ok) {
        slToast("ok", "已保存", r.data.plan.code + " · " + (creemId || "未绑价"));
      } else {
        slToast("err", "保存失败", (r.data && r.data.error) || "请求失败");
      }
    });
  });
});
