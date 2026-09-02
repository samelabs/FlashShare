// credits page: top-up & subscription checkout wiring (审计F1: 按钮此前零接线)
(function () {
  "use strict";

  function checkout(url, body, btn) {
    var orig = btn.textContent;
    btn.disabled = true;
    btn.textContent = "…";
    fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      credentials: "include",
      body: JSON.stringify(body)
    }).then(function (r) {
      return r.json().then(function (d) { return { ok: r.ok, data: d }; });
    }).then(function (res) {
      // 支付跳转必须整页跳转 (checkout_url 指向 Creem 托管页)
      if (res.ok && res.data && res.data.checkout_url) {
        window.location.href = res.data.checkout_url;
        return;
      }
      btn.disabled = false;
      btn.textContent = orig;
      var why = (res.data && res.data.error) || "error";
      alert(why === "plan_not_ready" || why === "package_not_ready" ? "Not available yet" : why);
    }).catch(function () {
      btn.disabled = false;
      btn.textContent = orig;
    });
  }

  // 充值 (一次性 package)
  var topupGrid = document.querySelector(".topup-grid");
  if (topupGrid) {
    topupGrid.addEventListener("click", function (e) {
      var btn = e.target.closest(".topup-buy");
      if (!btn) return;
      checkout("/api/topup/checkout", { package_id: Number(btn.dataset.pkg) }, btn);
    });
  }

  // 订阅 (周期 plan)
  var subGrid = document.querySelector(".sub-grid");
  if (subGrid) {
    subGrid.addEventListener("click", function (e) {
      var btn = e.target.closest(".sub-buy");
      if (!btn) return;
      checkout("/api/topup/subscribe", { plan_id: Number(btn.dataset.plan) }, btn);
    });
  }
})();
