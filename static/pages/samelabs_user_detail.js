/* ── Samelabs user detail actions (B2b-2) ────────────────────
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

  var resetBtn = wrap.querySelector('.btn-reset-pw');
  if (resetBtn) {
    resetBtn.dataset.act = 'reset-password';
    resetBtn.addEventListener('click', function () {
      twoStep(resetBtn, '确认重置密码？', function (r) {
        if (r.data.ok) {
          var code = document.getElementById('tempPassword');
          code.style.display = 'inline-block';
          code.textContent = r.data.temp_password;
          say('一次性临时密码（仅本次显示，已复制选择）——请立即交给用户；全部会话已失效，API key 不受影响', false);
        } else {
          say('失败: ' + r.data.error, true);
        }
      });
    });
  }
  var suspBtn = wrap.querySelector('.btn-suspend');
  if (suspBtn && !suspBtn.disabled) {
    suspBtn.dataset.act = 'suspend';
    suspBtn.addEventListener('click', function () {
      twoStep(suspBtn, '确认停用该账户？', function (r) {
        if (r.data.ok) { say('已停用（已写审计）', false); reload(); }
        else { say('失败: ' + r.data.error, true); }
      });
    });
  }
  var unsuspBtn = wrap.querySelector('.btn-unsuspend');
  if (unsuspBtn) {
    unsuspBtn.dataset.act = 'unsuspend';
    unsuspBtn.addEventListener('click', function () {
      twoStep(unsuspBtn, '确认恢复访问？', function (r) {
        if (r.data.ok) { say('已恢复（已写审计）', false); reload(); }
        else { say('失败: ' + r.data.error, true); }
      });
    });
  }
})();
