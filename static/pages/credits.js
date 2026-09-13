// credits page: top-up & membership actions
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

  function planChange(url, plan, btn, mode) {
    var orig = btn.textContent;
    btn.disabled = true;
    btn.textContent = "…";
    fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      credentials: "include",
      body: JSON.stringify({ plan: plan })
    }).then(function (r) {
      return r.json().then(function (d) { return { ok: r.ok, data: d }; });
    }).then(function (res) {
      if (res.ok && res.data && res.data.ok) {
        if (mode === "upgrade") {
          // Provider accepted the immediate plan change. The webhook is the
          // authority for Membership + Credits; reload to converge the UI.
          window.location.reload();
          return;
        }
        btn.textContent = window.t ? window.t("sub_current") : "✓";
        return;
      }
      btn.disabled = false;
      btn.textContent = orig;
      alert((res.data && res.data.error) || "error");
    }).catch(function () {
      btn.disabled = false;
      btn.textContent = orig;
    });
  }

  var topupGrid = document.querySelector(".topup-grid");
  if (topupGrid) {
    topupGrid.addEventListener("click", function (e) {
      var btn = e.target.closest(".topup-buy");
      if (!btn) return;
      checkout("/api/topup/checkout", { package_id: Number(btn.dataset.pkg) }, btn);
    });
  }

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
        planChange("/api/topup/plan-upgrade", up.dataset.plan, up, "upgrade");
        return;
      }
      var down = e.target.closest(".sub-downgrade");
      if (down) {
        planChange("/api/topup/plan-downgrade", down.dataset.plan, down, "downgrade");
      }
    });
  }
})();
