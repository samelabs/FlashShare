// 注册页密码不一致提示：表单内容来自 auth/_card.html 单源（无 id），
// 行为与浮层共用 window.bindPwMismatch（auth-modal.js 提供，全站加载）。
(function () {
  "use strict";
  const form = document.querySelector("form.auth-form[action='/auth/register']");
  if (form && window.bindPwMismatch) window.bindPwMismatch(form);
})();
