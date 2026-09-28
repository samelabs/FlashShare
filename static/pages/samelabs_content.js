/* ── Content governance: file takedown ───────────────────
   Two-step confirm → POST /samelabs/content/uploads/{id}/delete
   (sharing single point delete + audit), then drop the row. */
(function () {
  document.querySelectorAll('[data-del-upload]').forEach(function (b) {
    var armed = false, t = 0;
    b.addEventListener('click', function () {
      if (!armed) {
        armed = true; b.dataset.orig = b.textContent;
        b.textContent = '确认下架？';
        b.classList.add('sl-btn-danger');
        t = setTimeout(function () {
          if (armed) {
            armed = false; b.textContent = b.dataset.orig;
            b.classList.remove('sl-btn-danger');
          }
        }, 4000);
        return;
      }
      clearTimeout(t); b.disabled = true;
      slBusy(b, slPost('/samelabs/content/uploads/' + b.dataset.delUpload + '/delete', {}))
        .then(function (r) {
          if (r.ok && r.data.ok) {
            slToast('ok', '已下架', b.dataset.name + '（公开链接已删除，存储文件已提交删除；已写审计）');
            var tr = b.closest('tr');
            if (tr) tr.remove();
          } else {
            slToast('err', '下架失败', (r.data && r.data.error) || '');
            b.disabled = false; armed = false;
            b.textContent = b.dataset.orig || '下架';
            b.classList.remove('sl-btn-danger');
          }
        });
    });
  });
})();
