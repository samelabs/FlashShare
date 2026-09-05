// 市场页: 检索 + 多维过滤 (发现面) — 纯客户端过滤 (SSR 全量卡片 + data-* 维度),
// 无新增 API 依赖; 搜索框复用全站 tool_search_ph 文案。
(function () {
  "use strict";
  const input = document.getElementById("marketSearch");
  const filters = document.getElementById("marketFilters");
  const grid = document.getElementById("marketGrid");
  const none = document.getElementById("marketNone");
  if (!input || !filters || !grid) return;
  const cards = Array.from(grid.querySelectorAll(".mkt-card"));
  let activeFilter = "all";

  function apply() {
    const q = (input.value || "").trim().toLowerCase();
    let shown = 0;
    cards.forEach(c => {
      const origin = c.dataset.origin;
      const cost = parseInt(c.dataset.cost || "0", 10);
      let ok = true;
      if (activeFilter === "official") ok = origin === "official";
      else if (activeFilter === "community") ok = origin === "community";
      else if (activeFilter === "free") ok = cost === 0;
      else if (activeFilter === "paid") ok = cost > 0;
      if (ok && q) ok = (c.dataset.name || "").indexOf(q) >= 0;
      c.classList.toggle("hidden", !ok);
      if (ok) shown++;
    });
    if (none) none.classList.toggle("hidden", shown > 0);
  }

  let timer = null;
  input.addEventListener("input", () => {
    clearTimeout(timer);
    timer = setTimeout(apply, 120);
  });
  filters.querySelectorAll(".market-filter").forEach(btn => {
    btn.addEventListener("click", () => {
      filters.querySelectorAll(".market-filter").forEach(b => b.classList.remove("active"));
      btn.classList.add("active");
      activeFilter = btn.dataset.filter;
      apply();
    });
  });
})();
