/* ── Tool detail actions ─────────────────────────────────
   Same endpoints as the supply list (delist / presentation / pricing /
   delete); tool_key may carry '/' — URLs built via encodeURIComponent. */
(function () {
  var wrap = document.getElementById('toolActions');
  if (!wrap) return;
  var key = wrap.dataset.key;

  function toolUrl(action) {
    return '/samelabs/tools/' + encodeURIComponent(key) + '/' + action;
  }

  var delist = document.getElementById('toggleDelisted');
  if (delist) {
    delist.addEventListener('change', function () {
      var wantListed = delist.checked;
      delist.disabled = true;
      slPost(toolUrl('delist'), { delisted: !wantListed }).then(function (r) {
        delist.disabled = false;
        if (r.ok && r.data.ok) {
          delist.checked = !r.data.delisted;
          slToast('ok', r.data.delisted ? '已下架' : '已恢复上架', key);
        } else {
          delist.checked = !wantListed;
          slToast('err', '切换失败', (r.data && r.data.error) || '');
        }
      });
    });
  }

  var presForm = document.getElementById('presentationForm');
  if (presForm) {
    presForm.addEventListener('submit', function (e) {
      e.preventDefault();
      var sortOrder = parseInt(presForm.sort_order.value, 10);
      if (isNaN(sortOrder) || sortOrder < 0) {
        slToast('err', '排序值无效', '必须是非负整数'); return;
      }
      var btn = presForm.querySelector('button[type=submit]');
      slBusy(btn, slPost(toolUrl('presentation'), {
        featured: presForm.featured.checked, sort_order: sortOrder
      })).then(function (r) {
        if (r.ok && r.data.ok) slToast('ok', '已保存推荐设置', key);
        else slToast('err', '保存失败', (r.data && r.data.error) || '');
      });
    });
  }

  var pricingForm = document.getElementById('pricingForm');
  if (pricingForm) {
    pricingForm.addEventListener('submit', function (e) {
      e.preventDefault();
      var cost = parseInt(pricingForm.cost.value, 10);
      if (isNaN(cost) || cost < 0) { slToast('err', '单价无效', key); return; }
      var btn = pricingForm.querySelector('button[type=submit]');
      slBusy(btn, slPost(toolUrl('pricing'), {
        cost: cost, enabled: pricingForm.enabled.checked
      })).then(function (r) {
        if (r.ok && r.data.ok) slToast('ok', '已保存定价', key + ' · ' + cost + ' 积分/次');
        else slToast('err', '保存失败', (r.data && r.data.error) || '');
      });
    });
  }

  var delBtn = document.getElementById('delTool');
  if (delBtn) {
    var armed = false, t = 0;
    delBtn.addEventListener('click', function () {
      if (!armed) {
        armed = true; delBtn.textContent = '确认删除？';
        t = setTimeout(function () { armed = false; delBtn.textContent = '删除工具'; }, 4000);
        return;
      }
      clearTimeout(t); delBtn.disabled = true;
      slPost(toolUrl('delete'), {}).then(function (r) {
        if (r.ok && r.data.ok) {
          slToast('ok', '已删除', key);
          setTimeout(function () { location.href = '/samelabs/tools'; }, 600);
        } else {
          slToast('err', '删除失败', (r.data && r.data.error) || '');
          delBtn.disabled = false; armed = false; delBtn.textContent = '删除工具';
        }
      });
    });
  }
})();
