/* ── Tool Supply: pricing inline save + enable switch + HTTP create/delete ──
   tool_key may carry '/' (provider tools: owner/tool) — every URL segment
   is built through toolUrl() (encodeURIComponent). */
(function () {
  function toolUrl(key, action) {
    return '/samelabs/tools/' + encodeURIComponent(key) + '/' + action;
  }

  document.querySelectorAll('input[data-toggle-delisted]').forEach(function (cb) {
    cb.addEventListener('change', function () {
      var key = cb.dataset.toggleDelisted;
      var wantListed = cb.checked;           // checked = 上架中
      cb.disabled = true;
      slPost(toolUrl(key, 'delist'), { delisted: !wantListed }).then(function (r) {
        cb.disabled = false;
        if (r.ok && r.data.ok) {
          cb.checked = !r.data.delisted;
          slToast('ok', r.data.delisted ? '已下架' : '已恢复上架', key);
        } else {
          cb.checked = !wantListed;
          slToast('err', '切换失败', key + ' · ' + ((r.data && r.data.error) || ''));
        }
      });
    });
  });

  document.querySelectorAll('form[data-pricing]').forEach(function (f) {
    f.addEventListener('submit', function (e) {
      e.preventDefault();
      var tool = f.dataset.pricing;
      var cost = parseInt(f.cost.value, 10);
      if (isNaN(cost) || cost < 0) { slToast('err', '单价无效', tool); return; }
      var btn = f.querySelector('button[type=submit]');
      slBusy(btn, slPost(toolUrl(tool, 'pricing'),
                         { cost: cost, enabled: f.enabled.checked }))
        .then(function (r) {
          if (r.ok && r.data.ok) slToast('ok', '已保存', tool + ' · ' + cost + ' Credits');
          else slToast('err', '保存失败', tool + ' · ' + ((r.data && r.data.error) || ''));
        });
    });
  });

  document.querySelectorAll('[data-del]').forEach(function (b) {
    var armed = false, t = 0;
    b.addEventListener('click', function () {
      if (!armed) {
        armed = true; b.textContent = '确认删除?';
        t = setTimeout(function () { armed = false; b.textContent = '删除'; }, 4000);
        return;
      }
      clearTimeout(t); b.disabled = true;
      slPost(toolUrl(b.dataset.del, 'delete'), {})
        .then(function (r) {
          if (r.ok && r.data.ok) { slToast('ok', '已删除', b.dataset.del); setTimeout(function () { location.reload(); }, 600); }
          else { slToast('err', '删除失败', (r.data && r.data.error) || ''); b.disabled = false; armed = false; b.textContent = '删除'; }
        });
    });
  });

  var modal = document.getElementById('createHttpModal');
  var btnCreate = document.getElementById('btnCreateHttp');
  if (!modal || !btnCreate) return;
  var form = document.getElementById('createHttpForm');

  btnCreate.addEventListener('click', function () {
    form.reset();
    if (typeof modal.showModal === 'function') modal.showModal();
    else modal.setAttribute('open', '');
  });
  document.getElementById('createHttpCancel').addEventListener('click', function () {
    if (typeof modal.close === 'function') modal.close(); else modal.removeAttribute('open');
  });

  document.getElementById('createHttpSubmit').addEventListener('click', function () {
    var paramsDecl = null;
    var paramsRaw = (form.params.value || '').trim();
    if (paramsRaw) {
      try {
        paramsDecl = JSON.parse(paramsRaw);
        if (!Array.isArray(paramsDecl)) throw new Error('not array');
      } catch (e) {
        slToast('err', '参数声明格式错误', '必须是 JSON 数组');
        return;
      }
    }
    var payload = {
      tool_id: form.tool_id.value.trim(),
      label: form.label.value.trim(),
      url: form.url.value.trim(),
      timeout_s: parseInt(form.timeout_s.value, 10) || 60,
      auth_header: form.auth_header.value.trim(),
      auth_token: form.auth_token.value,
      accept: form.accept.value.trim(),
      files_min: parseInt(form.files_min.value, 10) || 0,
      files_max: parseInt(form.files_max.value, 10),
      outputs: ["text", "json", "file"].filter(function (k) {
        var el = form["out_" + k];
        return el && el.checked;
      }),
      params: paramsDecl,
      cost: parseInt(form.cost.value, 10) || 0
    };
    var btn = this;
    slBusy(btn, slPost('/samelabs/tools/create', payload)).then(function (r) {
      if (r.ok && r.data.ok) {
        slToast('ok', '已创建', r.data.tool_key);
        setTimeout(function () { location.reload(); }, 600);
      } else {
        var why = (r.data && r.data.error) || '请求失败';
        if (why === 'duplicate_tool') why = '工具 ID 已存在';
        if (why === 'bad_tool_id') why = '工具 ID 格式非法（a-z0-9_）';
        slToast('err', '创建失败', why);
      }
    });
  });
})();
