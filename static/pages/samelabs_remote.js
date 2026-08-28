document.getElementById('remoteCreate').addEventListener('submit', function (e) {
  e.preventDefault();
  var f = e.target;
  var payload = {
    tool_id: f.tool_id.value.trim(),
    label: f.label.value.trim(),
        url: f.url.value.trim(),
    timeout_s: parseInt(f.timeout_s.value, 10) || 60,
    auth_header: f.auth_header.value.trim(),
    auth_token: f.auth_token.value.trim(),
    accept: f.accept.value.trim(),
    cost: parseInt(f.cost.value, 10) || 0,
    output_filename: f.output_filename.value.trim(),
    output_content_type: f.output_content_type.value.trim(),
  };
  slBusy(f.querySelector('button[type=submit]'), slPost('/samelabs/remote/create', payload)).then(function (r) {
    if (r.ok && r.data.ok) {
      slToast('ok', '已创建', payload.tool_id + ' · 刷新页面查看');
      setTimeout(function () { location.reload(); }, 600);
    } else {
      var why = { bad_tool_id: 'tool_id 非法(小写字母开头,2-41位)',
                  bad_url: 'URL 非法',
                  bad_timeout: '超时需 5-300',
                  bad_output_filename: '输出名扩展名不在白名单',
                  duplicate_or_db: 'tool_id 已存在或写入失败' }[r.data.error] || r.data.error || '请求失败';
      slToast('err', '创建失败', why);
    }
  });
});

/* 删除两态: ghost[删除] → danger[确认删除] → POST → 行移除 */
document.querySelectorAll('.btn-del-remote').forEach(function (btn) {
  var armed = false;
  btn.addEventListener('click', function () {
    var tool = btn.dataset.tool;
    if (!armed) {
      armed = true;
      btn.className = 'sl-btn sl-btn-danger sl-btn-sm btn-del-remote';
      btn.textContent = '确认删除';
      setTimeout(function () {
        armed = false;
        btn.className = 'sl-btn sl-btn-ghost sl-btn-sm btn-del-remote';
        btn.textContent = '删除';
      }, 3000);
      return;
    }
    slBusy(btn, slPost('/samelabs/remote/' + tool + '/delete', {})).then(function (r) {
      if (r.ok && r.data.ok) {
        var row = document.querySelector('tr[data-tool="' + tool + '"]');
        if (row) row.remove();
        slToast('ok', '已删除', tool + ' · 定价已停用');
      } else {
        slToast('err', '删除失败', r.data.error || '请求失败');
      }
    });
  });
});
