/* ── Operator console writes: 封禁/解封 · 分享下架 · 工具审核下架 · 会员对账 ──
   两态确认 (与积分调整同一交互): 首次点击进入 "确认…" 态, 3 秒内再点才提交;
   结果统一 toast, 成功后刷新当前页。 */
(function () {
  var OPS = {
    status: {
      url: function (b) { return '/samelabs/users/' + b.dataset.uid + '/status'; },
      body: function (b) { return { status: b.dataset.status }; },
      confirm: function (b) { return b.dataset.status === 'suspended' ? '确认封禁' : '确认解封'; },
      done: function (b) { return b.dataset.status === 'suspended' ? '已封禁' : '已解封'; }
    },
    takedown: {
      url: function (b) { return '/samelabs/uploads/' + encodeURIComponent(b.dataset.pid) + '/takedown'; },
      body: function () { return {}; },
      confirm: function () { return '确认下架'; },
      done: function (b) { return '已下架 ' + b.dataset.pid; }
    },
    hold: {
      url: function () { return '/samelabs/tool-holds'; },
      body: function (b) { return { tool_key: b.dataset.tool }; },
      confirm: function () { return '确认下架'; },
      done: function (b) { return '已下架 ' + b.dataset.tool; }
    },
    release: {
      url: function () { return '/samelabs/tool-holds/release'; },
      body: function (b) { return { tool_key: b.dataset.tool }; },
      confirm: function () { return '确认解除'; },
      done: function (b) { return '已解除 ' + b.dataset.tool; }
    },
    reconcile: {
      url: function (b) { return '/samelabs/users/' + b.dataset.uid + '/reconcile'; },
      body: function () { return {}; },
      confirm: function () { return '确认对账'; },
      done: function () { return '对账完成'; }
    }
  };

  document.querySelectorAll('[data-op]').forEach(function (btn) {
    var op = OPS[btn.dataset.op];
    if (!op) return;
    var label = btn.textContent;
    var armed = null;
    btn.addEventListener('click', function () {
      if (!armed) {
        btn.textContent = op.confirm(btn);
        armed = setTimeout(function () { btn.textContent = label; armed = null; }, 3000);
        return;
      }
      clearTimeout(armed); armed = null;
      btn.textContent = label;
      slBusy(btn, slPost(op.url(btn), op.body(btn))).then(function (r) {
        if (r.ok && r.data.ok) {
          var sub = btn.dataset.op === 'reconcile'
            ? '检查 ' + r.data.checked + ' · 补发 ' + slFmt(r.data.credits) + ' 积分' : '';
          slToast('ok', op.done(btn), sub);
          setTimeout(function () { window.location.reload(); }, 700);
        } else {
          slToast('err', '操作失败', (r.data && r.data.error) || '请求失败');
        }
      });
    });
  });
})();
