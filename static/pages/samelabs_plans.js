// 档位管理: 行内全字段保存 + 上下架 + 新建
function _planRowPayload(tr) {
  var num = function (f) { return parseInt(tr.querySelector('[data-field="' + f + '"]').value, 10) || 0; };
  var txt = function (f) { return tr.querySelector('[data-field="' + f + '"]').value.trim(); };
  var arr = function (f) {
    var v = txt(f);
    if (!v) return [];
    return v.split(",").map(function (x) { return parseInt(x.trim(), 10); }).filter(function (x) { return !isNaN(x); });
  };
  return {
    name: txt("name"),
    price_cents: num("price_cents"),
    monthly_credits: num("monthly_credits"),
    period_days: num("period_days") || 30,
    creem_product_id: txt("creem_product_id") || null,
    ingress_files_24h: num("ingress_files_24h"),
    ingress_bytes_24h: num("ingress_mb") * 1048576,
    expiry_days: arr("expiry_days"),
    download_limits: arr("download_limits"),
    sort_order: num("sort_order")
  };
}

document.querySelectorAll(".plan-save").forEach(function (btn) {
  btn.addEventListener("click", function () {
    var tr = btn.closest("tr");
    var payload = _planRowPayload(tr);
    slBusy(btn, slPost("/samelabs/plans/" + btn.dataset.plan, payload, "PATCH")).then(function (r) {
      if (r.ok && r.data.ok) {
        slToast("ok", "已保存", r.data.plan.code + " · $" + (r.data.plan.price_cents / 100).toFixed(2));
        setTimeout(function () { window.location.reload(); }, 600);
      } else {
        slToast("err", "保存失败", (r.data && r.data.error) || "请求失败");
      }
    });
  });
});

document.querySelectorAll(".plan-toggle").forEach(function (btn) {
  btn.addEventListener("click", function () {
    var active = btn.dataset.active === "1";
    slBusy(btn, slPost("/samelabs/plans/" + btn.dataset.plan, { active: !active }, "PATCH")).then(function (r) {
      if (r.ok && r.data.ok) {
        window.location.reload();
      } else {
        slToast("err", active ? "下架失败" : "上架失败", (r.data && r.data.error) || "请求失败");
      }
    });
  });
});

document.getElementById("planForm").addEventListener("submit", function (e) {
  e.preventDefault();
  var num = function (id) { return parseInt(document.getElementById(id).value, 10) || 0; };
  var arr = function (id) {
    var v = document.getElementById(id).value.trim();
    return v ? v.split(",").map(function (x) { return parseInt(x.trim(), 10); }).filter(function (x) { return !isNaN(x); }) : [];
  };
  var payload = {
    code: document.getElementById("planCode").value.trim().toLowerCase(),
    name: document.getElementById("planName").value.trim(),
    monthly_credits: num("planCredits"),
    price_cents: num("planPrice"),
    period_days: num("planPeriod") || 30,
    creem_product_id: document.getElementById("planProduct").value.trim() || null,
    expiry_days: arr("planExpiry"),
    download_limits: arr("planDownloads")
  };
  var btn = document.getElementById("planCreate");
  slBusy(btn, slPost("/samelabs/plans", payload, "POST")).then(function (r) {
    if (r.ok && r.data.ok) {
      slToast("ok", "已创建", r.data.plan.code + " (权益未配齐前不可售)");
      setTimeout(function () { window.location.reload(); }, 600);
    } else {
      slToast("err", "创建失败", (r.data && r.data.error) || "请求失败");
    }
  });
});
