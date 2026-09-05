/* 保存单价 / 启停 — 行内更新, 不 reload */
function savePricing(tool, cost, enabled) {
  return slPost('/samelabs/tools/' + tool + '/pricing', { cost: cost, enabled: enabled });
}

document.querySelectorAll('.btn-save-pricing').forEach(function (btn) {
  btn.addEventListener('click', function () {
    var tool = btn.dataset.tool;
    var row = document.querySelector('tr[data-tool="' + tool + '"]');
    var input = row.querySelector('.tool-cost');
    var cost = parseInt(input.value, 10);
    if (isNaN(cost) || cost < 0) {
      slToast('err', '单价无效', tool + ' · 需为 0 – 1000000 的整数');
      input.focus();
      return;
    }
    var enabled = row.querySelector('.tool-enabled .sl-badge').textContent.indexOf('启用') !== -1;
    var featured = row.querySelector('.tool-featured').checked;
    var sortOrder = parseInt(row.querySelector('.tool-sort').value, 10) || 0;
    // 单价 + 推荐/排序一次保存 (遴选呈现序的权威入口)
    slBusy(btn, savePricing(tool, cost, enabled)).then(function (r) {
      if (r.ok && r.data.ok) {
        return slPost('/samelabs/tools/' + encodeURIComponent(tool) + '/presentation',
                      { featured: featured, sort_order: sortOrder }, 'PATCH')
          .then(function (pr) {
            if (pr.ok && pr.data.ok) slToast('ok', '已保存', tool + ' · 推荐=' + (featured ? '是' : '否') + ' · 排序=' + sortOrder);
            else slToast('err', '推荐/排序保存失败', tool);
          });
      }
      slToast('err', '保存失败', tool + ' · ' + (r.data.error || '请求失败'));
    });
  });
});

document.querySelectorAll('.btn-toggle-tool').forEach(function (btn) {
  btn.addEventListener('click', function () {
    var tool = btn.dataset.tool;
    var nowEnabled = btn.dataset.enabled === 'true';
    var input = document.querySelector('.tool-cost[data-tool="' + tool + '"]');
    var cost = parseInt(input.value, 10) || 0;
    slBusy(btn, savePricing(tool, cost, !nowEnabled)).then(function (r) {
      if (r.ok && r.data.ok) {
        var row = document.querySelector('tr[data-tool="' + tool + '"]');
        var badge = row.querySelector('.tool-enabled .sl-badge');
        var on = r.data.pricing.enabled;
        badge.className = 'sl-badge ' + (on ? 'sl-b-ok' : 'sl-b-muted');
        badge.textContent = on ? '启用' : '停用';
        btn.dataset.enabled = on ? 'true' : 'false';
        btn.textContent = on ? '停用' : '启用';
        slToast('ok', on ? '工具已启用' : '工具已停用', tool);
      } else {
        slToast('err', '操作失败', tool + ' · ' + (r.data.error || '请求失败'));
      }
    });
  });
});
