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

  // 订阅 / 升级 / 降级 (canonical tiers: plus / pro)
  var subGrid = document.querySelector(".sub-grid");
  if (subGrid) {
    subGrid.addEventListener("click", function (e) {
      var buy = e.target.closest(".sub-buy");
      if (buy) {
        checkout("/api/topup/subscribe", { plan_id: Number(buy.dataset.plan) }, buy);
        return;
      }
      var up = e.target.closest(".sub-upgrade");
      if (up) {
        // upgrade 不是 checkout 跳转: provider 升级就地受理, 返回
        // {ok, upgrade:"initiated"} 无 checkout_url — 走独立处理, 不进
        // checkout() 的 error-alert 分支。
        var uorig = up.textContent;
        up.disabled = true;
        up.textContent = "…";
        fetch("/api/topup/plan-upgrade", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          credentials: "include",
          body: JSON.stringify({ plan: up.dataset.plan })
        }).then(function (r) { return r.json().then(function (d) { return { ok: r.ok, data: d }; }); }).then(function (res) {
          if (res.ok && res.data && res.data.ok) {
            up.textContent = window.t ? window.t("sub_upgrade_done") : "✓";
            setTimeout(function () { window.location.reload(); }, 1200);
            return;
          }
          up.disabled = false;
          up.textContent = uorig;
          var why = (res.data && res.data.error) || "error";
          alert(why === "plan_not_ready" ? (window.t ? window.t("sub_soon") : "Not available yet") : why);
        }).catch(function () {
          up.disabled = false;
          up.textContent = uorig;
        });
        return;
      }
      var down = e.target.closest(".sub-downgrade");
      if (down) {
        // next-renewal 语义: 成功后不跳转, 提示已排期
        var orig = down.textContent;
        down.disabled = true;
        down.textContent = "…";
        fetch("/api/topup/plan-downgrade", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          credentials: "include",
          body: JSON.stringify({ plan: down.dataset.plan })
        }).then(function (r) { return r.json(); }).then(function (d) {
          if (d && d.ok) {
            // next-renewal 语义: 排期成功 ≠ 立即切换, 显排期态并禁重复
            down.disabled = true;
            down.textContent = window.t ? window.t("sub_downgrade_done") : "✓";
            return;
          }
          down.disabled = false;
          down.textContent = orig;
          alert((d && d.error) || "error");
        }).catch(function () {
          down.disabled = false;
          down.textContent = orig;
        });
      }
    });
  }
})();
