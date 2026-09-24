// 积分包: 只读卡片 + 配置抽屉
(function () {
  "use strict";

  var PKGS = window.__SL_PKGS__ || [];
  var modal = document.getElementById("pkgModal");
  var editing = null;

  function $(id) { return document.getElementById(id); }

  function fill(p) {
    var codeField = document.querySelector(".tier-field-create");
    if (codeField) codeField.classList.toggle("hidden", !!p);
    $("g_code").value = p ? p.code : "";
    $("g_code").disabled = !!p;
    $("g_name").value = p ? p.name : "";
    $("g_credits").value = p ? p.credits : 0;
    $("g_price_cents").value = p ? p.price_cents : 0;
    $("g_active").checked = p ? !!p.active : true;
    $("pkgEditorCode").textContent = p ? p.code : "新 code";
    $("pkgEditorTitle").textContent = p ? "积分包配置" : "新建积分包";
  }

  function open(p) {
    editing = p ? p.id : null;
    fill(p);
    modal.classList.remove("hidden");
  }

  function close() {
    modal.classList.add("hidden");
    editing = null;
  }

  document.querySelectorAll(".tier-card[data-pkg]").forEach(function (card) {
    card.addEventListener("click", function () {
      var id = parseInt(card.dataset.pkg, 10);
      open(PKGS.find(function (p) { return p.id === id; }));
    });
  });

  var add = document.getElementById("pkgAdd");
  if (add) add.addEventListener("click", function () { open(null); });
  $("pkgModalClose").addEventListener("click", close);
  modal.addEventListener("click", function (e) { if (e.target === modal) close(); });

  $("pkgSave").addEventListener("click", function () {
    var btn = $("pkgSave");
    var body = {
      name: $("g_name").value.trim(),
      credits: parseInt($("g_credits").value, 10) || 0,
      price_cents: parseInt($("g_price_cents").value, 10) || 0,
      active: $("g_active").checked
    };
    if (editing === null) {
      body.code = $("g_code").value.trim().toLowerCase();
      if (!body.code) { slToast("err", "缺 code", ""); return; }
    }
    if (!body.name) { slToast("err", "缺名称", ""); return; }
    if (body.credits < 1) { slToast("err", "积分须 ≥ 1", ""); return; }
    var url = editing === null ? "/samelabs/commerce/products/packages" : "/samelabs/commerce/products/packages/" + editing;
    slBusy(btn, slPost(url, body, editing === null ? "POST" : "PATCH")).then(function (r) {
      if (r.ok && r.data.ok) {
        slToast("ok", editing === null ? "已创建" : "已保存", r.data.package.code + " · $" + (r.data.package.price_cents / 100).toFixed(2));
        setTimeout(function () { window.location.reload(); }, 600);
      } else {
        var why = r.data && r.data.error;
        if (why === "code_taken") why = "code 已存在";
        if (why === "credits_per_usd_exceeded") why = "每 USD 至多 1000 积分 (积分 ≤ 价格分 × 10)";
        if (why === "currency_not_usd") why = "仅支持 USD 定价";
        slToast("err", "保存失败", why || "请求失败");
      }
    });
  });
})();
