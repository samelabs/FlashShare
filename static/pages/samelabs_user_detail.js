/* ── Samelabs user detail actions  ────────────────────
   reset-password  POST /samelabs/users/{id}/reset-password
                    → one-time temp password, displayed ONCE
   suspend         POST /samelabs/users/{id}/suspend
   unsuspend       POST /samelabs/users/{id}/unsuspend
   Every financial/identity action uses two-step confirm + toast. */
(function () {
  var wrap = document.getElementById('accountActions');
  if (!wrap) return;
  var uid = wrap.dataset.uid;
  var msg = document.getElementById('actionMsg');

  function say(text, bad) {
    msg.textContent = text;
    msg.style.color = bad ? 'var(--danger, #b91c1c)' : 'var(--muted, #536471)';
  }
  function reload() { setTimeout(function () { location.reload(); }, 1600); }
  function twoStep(btn, armText, fn) {
    if (btn.dataset.armed !== '1') {
      btn.dataset.armed = '1';
      btn.dataset.orig = btn.textContent;
      btn.textContent = armText;
      btn.classList.add('sl-btn-danger');
      setTimeout(function () {
        if (btn.dataset.armed === '1') {
          btn.dataset.armed = '';
          btn.textContent = btn.dataset.orig;
          btn.classList.remove('sl-btn-danger');
        }
      }, 5000);
      return;
    }
    btn.dataset.armed = '';
    btn.disabled = true;
    slPost('/samelabs/users/' + uid + '/' + btn.dataset.act, {})
      .then(function (r) {
        btn.disabled = false;
        btn.classList.remove('sl-btn-danger');
        btn.textContent = btn.dataset.orig;
        fn(r);
      });
  }

  var resetBtn = wrap.querySelector('.js-reset-pw');
  if (resetBtn) {
    resetBtn.dataset.act = 'reset-password';
    resetBtn.addEventListener('click', function () {
      twoStep(resetBtn, '确认重置密码？', function (r) {
        if (r.data.ok) {
          var code = document.getElementById('tempPassword');
          code.style.display = 'inline-block';
          code.textContent = r.data.temp_password;
          say('一次性临时密码（仅本次显示）——请立即交给用户；全部登录会话已失效，API key 不受影响', false);
        } else {
          say('失败: ' + r.data.error, true);
        }
      });
    });
  }
  var suspBtn = wrap.querySelector('.js-suspend');
  if (suspBtn && !suspBtn.disabled) {
    suspBtn.dataset.act = 'suspend';
    suspBtn.addEventListener('click', function () {
      twoStep(suspBtn, '确认停用该账户？', function (r) {
        if (r.data.ok) { say('已停用（已写审计）', false); reload(); }
        else { say('失败: ' + r.data.error, true); }
      });
    });
  }
  var unsuspBtn = wrap.querySelector('.js-unsuspend');
  if (unsuspBtn) {
    unsuspBtn.dataset.act = 'unsuspend';
    unsuspBtn.addEventListener('click', function () {
      twoStep(unsuspBtn, '确认恢复访问？', function (r) {
        if (r.data.ok) { say('已恢复（已写审计）', false); reload(); }
        else { say('失败: ' + r.data.error, true); }
      });
    });
  }

  /* API key deletion — two-step confirm, same visual pattern */
  var keysPanel = document.getElementById('apiKeysPanel');
  if (keysPanel) {
    keysPanel.querySelectorAll('[data-del-key]').forEach(function (b) {
      var armed = false, t = 0;
      b.addEventListener('click', function () {
        if (!armed) {
          armed = true; b.dataset.orig = b.textContent;
          b.textContent = '确认删除？';
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
        slBusy(b, slPost('/samelabs/users/' + keysPanel.dataset.uid +
                         '/keys/' + b.dataset.delKey + '/delete', {}))
          .then(function (r) {
            if (r.ok && r.data.ok) {
              slToast('ok', '已删除 API key', b.dataset.name + '（立即生效，已写审计）');
              var tr = b.closest('tr');
              if (tr) tr.remove();
            } else {
              slToast('err', '删除失败', (r.data && r.data.error) || '');
              b.disabled = false; armed = false;
              b.textContent = b.dataset.orig || '删除';
              b.classList.remove('sl-btn-danger');
            }
          });
      });
    });
  }
})();
