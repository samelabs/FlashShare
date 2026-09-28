// /pricing: credit-pack & subscription checkout wiring
(function () {
  "use strict";

  // Signed out: any buy button opens the login modal; after login the page
  // reloads so every plan shows the viewer's own action.
  document.addEventListener("click", function (e) {
    if (window.__USER__) return;
    var btn = e.target.closest(".topup-buy, .sub-buy");
    if (!btn || !window.openAuthModal) return;
    e.preventDefault();
    e.stopImmediatePropagation();
    window.openAuthModal({ mode: "register", onSuccess: function () { window.location.reload(); } });
  }, true);

  // Purchase intent identity is server-side Order.id: a retry of the
  // same package/plan converges on the same pending Order (same
  // provider request_id=order:<id>) — no client identity is needed.
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
      // 支付跳转必须整页跳转 (checkout_url 指向 provider 托管页)。
      if (res.ok && res.data && res.data.checkout_url) {
        window.location.href = res.data.checkout_url;
        return;
      }
      btn.disabled = false;
      btn.textContent = orig;
      // unified error boundary (common.js _errm): every checkout error
      // code maps to a localized message; unknown codes get the generic
      // failure text — a raw code never reaches the user
      var why = (res.data && res.data.error) || "";
      alert(window._errm(why));
    }).catch(function () {
      btn.disabled = false;
      btn.textContent = orig;
    });
  }

  // 充值 (一次性 package)
  var topupGrid = document.querySelector(".pr-packs");
  if (topupGrid) {
    topupGrid.addEventListener("click", function (e) {
      var btn = e.target.closest(".topup-buy");
      if (!btn) return;
      checkout("/api/topup/checkout", { package_id: Number(btn.dataset.pkg) }, btn);
    });
  }

  // 会员卡片: 服务端 action 已定 (subscribe/upgrade/downgrade), 前端只按
  // 按钮类分流端点 — 档名语义不进 JS。
  var subGrid = document.querySelector(".pr-plans");
  if (subGrid) {
    subGrid.addEventListener("click", function (e) {
      var buy = e.target.closest(".sub-buy");
      if (buy) {
        checkout("/api/topup/subscribe", { plan_id: Number(buy.dataset.plan) }, buy);
        return;
      }
      var up = e.target.closest(".sub-upgrade");
      if (up) {
        // upgrade 非 checkout 跳转: provider 就地受理, 返回 {ok, upgrade}
        // 无 checkout_url — 独立处理, 不进 checkout() 的 error-alert 分支。
        var uorig = up.textContent;
        up.disabled = true;
        up.textContent = "…";
        fetch("/api/topup/plan-upgrade", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          credentials: "include",
          body: JSON.stringify({ plan: up.dataset.plan })
        }).then(function (r) { return r.json(); }).then(function (d) {
          if (d && d.ok) {
            up.textContent = window.t ? window.t("sub_upgrade_done") : "✓";
            setTimeout(function () { window.location.reload(); }, 1200);
            return;
          }
          up.disabled = false;
          up.textContent = uorig;
          alert(window._errm(d && d.error));
        }).catch(function () {
          up.disabled = false;
          up.textContent = uorig;
        });
        return;
      }
      var down = e.target.closest(".sub-downgrade");
      if (down) {
        // next-renewal 语义: 排期成功 ≠ 立即切换 — 显排期态并禁重复
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
            down.textContent = window.t ? window.t("sub_downgrade_done") : "✓";
            return;
          }
          down.disabled = false;
          down.textContent = orig;
          alert(window._errm(d && d.error));
        }).catch(function () {
          down.disabled = false;
          down.textContent = orig;
        });
      }
    });
  }
})();
