// 档位管理: 只读卡片 + 配置抽屉
(function () {
  "use strict";

  var PLANS = window.__SL_PLANS__ || [];
  var modal = document.getElementById("tierModal");
  var editing = null; // null = 新建

  function $(id) { return document.getElementById(id); }

  function arrVal(id) {
    var v = $(id).value.trim();
    if (!v) return [];
    return v.split(",").map(function (x) { return parseInt(x.trim(), 10); })
      .filter(function (x) { return !isNaN(x); });
  }

  function fill(p) {
    $("f_name").value = p ? p.name : "";
    $("f_price_cents").value = p ? p.price_cents : 0;
    $("f_monthly_credits").value = p ? p.monthly_credits : 0;
    $("f_period_days").value = p ? p.period_days : 30;
    $("f_sort_order").value = p ? p.sort_order : 0;
    $("f_creem_product_id").value = p ? (p.creem_product_id || "") : "";
    $("f_ingress_files_24h").value = p ? p.ingress_files_24h : 0;
    $("f_ingress_mb").value = p ? Math.floor((p.ingress_bytes_24h || 0) / 1048576) : 0;
    $("f_expiry_days").value = p ? (p.expiry_days || []).join(",") : "";
    $("f_download_limits").value = p ? (p.download_limits || []).join(",") : "";
    $("f_active").checked = p ? !!p.active : true;
    $("tierEditorCode").textContent = p ? p.code : "新 code";
    $("tierEditorTitle").textContent = p ? "档位配置" : "新建档位";
    $("tierEditorHint").textContent = p
      ? (p.member_count + " 个当前订阅" + (p.pending_count ? " · " + p.pending_count + " 个排期切换" : ""))
      : "code 创建后不可改";
    // code 只在新建时可填
    var codeField = document.querySelector(".tier-field-create");
    var codeInput = $("f_code");
    if (codeField) codeField.classList.toggle("hidden", !!p);
    if (codeInput) codeInput.disabled = !!p;
    if (!p && codeInput) codeInput.value = "";
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

  function payload() {
    var body = {
      name: $("f_name").value.trim(),
      price_cents: parseInt($("f_price_cents").value, 10) || 0,
      monthly_credits: parseInt($("f_monthly_credits").value, 10) || 0,
      period_days: parseInt($("f_period_days").value, 10) || 30,
      sort_order: parseInt($("f_sort_order").value, 10) || 0,
      creem_product_id: $("f_creem_product_id").value.trim() || null,
      ingress_files_24h: parseInt($("f_ingress_files_24h").value, 10) || 0,
      ingress_bytes_24h: (parseInt($("f_ingress_mb").value, 10) || 0) * 1048576,
      expiry_days: arrVal("f_expiry_days"),
      download_limits: arrVal("f_download_limits"),
      active: $("f_active").checked
    };
    if (editing === null) {
      body.code = $("f_code").value.trim().toLowerCase();
    }
    return body;
  }

  document.querySelectorAll(".tier-card[data-plan]").forEach(function (card) {
    card.addEventListener("click", function () {
      var id = parseInt(card.dataset.plan, 10);
      open(PLANS.find(function (p) { return p.id === id; }));
    });
  });

  var add = document.getElementById("tierAdd");
  if (add) add.addEventListener("click", function () { open(null); });
  $("tierModalClose").addEventListener("click", close);
  modal.addEventListener("click", function (e) { if (e.target === modal) close(); });

  $("tierSave").addEventListener("click", function () {
    var btn = $("tierSave");
    var body = payload();
    if (editing === null && !body.code) { slToast("err", "缺 code", "新档位需要唯一 code"); return; }
    if (!body.name) { slToast("err", "缺名称", ""); return; }
    var url = editing === null ? "/samelabs/plans" : "/samelabs/plans/" + editing;
    slBusy(btn, slPost(url, body, editing === null ? "POST" : "PATCH")).then(function (r) {
      if (r.ok && r.data.ok) {
        slToast("ok", editing === null ? "已创建" : "已保存", r.data.plan.code + " · $" + (r.data.plan.price_cents / 100).toFixed(2));
        setTimeout(function () { window.location.reload(); }, 600);
      } else {
        var why = r.data && r.data.error;
        if (why === "code_taken") why = "code 已存在";
        if (why === "invalid_request") why = "字段非法 (检查权益值域)";
        slToast("err", "保存失败", why || "请求失败");
      }
    });
  });
})();
