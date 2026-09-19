// credits page: top-up & subscription checkout wiring
(function () {
  "use strict";

  // ── payment identity (client-generated, sessionStorage lifetime) ──
  // One logical checkout = one stable ref, born before the first POST,
  // reused across network errors / checkout_unresolved / reload / user
  // retry. Cleared on definitive success (?paid=1 return) or a
  // definitive closed failure. Keyed per purchase item so buying a
  // different package/plan never collides with a pending ref.
  function newPaymentRef() {
    var u = (window.crypto && crypto.randomUUID)
      ? crypto.randomUUID().replace(/-/g, "")
      : Array.from(crypto.getRandomValues(new Uint8Array(16)))
          .map(function (b) { return b.toString(16).padStart(2, "0"); }).join("");
    return "pay:" + u;
  }
  function refKey(kind, id) { return "payref:" + kind + ":" + id; }
  function getPaymentRef(kind, id) {
    try { return sessionStorage.getItem(refKey(kind, id)); } catch (e) { return null; }
  }
  function ensurePaymentRef(kind, id) {
    var r = getPaymentRef(kind, id);
    if (!r) {
      r = newPaymentRef();
      try { sessionStorage.setItem(refKey(kind, id), r); } catch (e) { /* private mode */ }
    }
    return r;
  }
  function clearPaymentRef(kind, id) {
    try { sessionStorage.removeItem(refKey(kind, id)); } catch (e) { /* noop */ }
  }
  function clearAllPaymentRefs() {
    try {
      Object.keys(sessionStorage)
        .filter(function (k) { return k.indexOf("payref:") === 0; })
        .forEach(function (k) { sessionStorage.removeItem(k); });
    } catch (e) { /* noop */ }
  }
  // Provider success returns to ?paid=1 — the logical purchase is
  // closed; every pending payment identity is released.
  if (window.location.search.indexOf("paid=1") !== -1) {
    clearAllPaymentRefs();
  }
  // Definitive closed failures release THIS item's ref (a retry is a
  // new logical purchase); unresolved/network failures keep it.
  var CLOSED_FAILURES = ["idempotency_conflict", "checkout_failed",
                         "checkout_expired", "checkout_closed"];

  function checkout(url, body, kind, itemId, btn) {
    var orig = btn.textContent;
    btn.disabled = true;
    btn.textContent = "…";
    var clientRef = ensurePaymentRef(kind, itemId);
    fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      credentials: "include",
      body: JSON.stringify(Object.assign({}, body, { client_ref: clientRef }))
    }).then(function (r) {
      return r.json().then(function (d) { return { ok: r.ok, data: d }; });
    }).then(function (res) {
      // 支付跳转必须整页跳转 (checkout_url 指向 provider 托管页)。
      // ref 保留: 用户可能 back / 导航异常, provider 侧仍是同一 checkout。
      if (res.ok && res.data && res.data.checkout_url) {
        window.location.href = res.data.checkout_url;
        return;
      }
      // definitive closed → 释放当前 ref; unresolved/network → 保留
      var errKey = res.data && res.data.error;
      if (CLOSED_FAILURES.indexOf(errKey) !== -1) {
        clearPaymentRef(kind, itemId);
      }
      btn.disabled = false;
      btn.textContent = orig;
      var why = (res.data && res.data.error) || "error";
      // 包未就绪与计划未就绪分键: 充值包错误不得显示会员文案
      if (why === "package_not_ready") {
        alert(window.t ? window.t("topup_soon") : "Not available yet");
      } else if (why === "plan_not_ready") {
        alert(window.t ? window.t("sub_soon") : "Not available yet");
      } else {
        alert(why);
      }
    }).catch(function () {
      // network loss: KEEP the ref — the server may have succeeded;
      // a retry with the same ref converges on the same order.
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
      checkout("/api/topup/checkout", { package_id: Number(btn.dataset.pkg) },
               "package", btn.dataset.pkg, btn);
    });
  }

  // 会员卡片: 服务端 action 已定 (subscribe/upgrade/downgrade), 前端只按
  // 按钮类分流端点 — 档名语义不进 JS。
  var subGrid = document.querySelector(".sub-grid");
  if (subGrid) {
    subGrid.addEventListener("click", function (e) {
      var buy = e.target.closest(".sub-buy");
      if (buy) {
        checkout("/api/topup/subscribe", { plan_id: Number(buy.dataset.plan) },
                 "plan", buy.dataset.plan, buy);
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
          var uwhy = d && d.error;
          alert(uwhy === "plan_not_ready"
            ? (window.t ? window.t("sub_soon") : "Not available yet")
            : (uwhy || "error"));
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
          alert((d && d.error) || "error");
        }).catch(function () {
          down.disabled = false;
          down.textContent = orig;
        });
      }
    });
  }
})();
