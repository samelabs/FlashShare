// ── security page: password-match feedback (41-18) ──
// Visible explanatory text + localized constraint message; never a bare
// setCustomValidity('x') sentinel.
const _pw = document.getElementById('newPw');
const _pwc = document.getElementById('newPwConfirm');
const _hint = document.getElementById('pwMismatch');
function checkPw() {
  const mismatch = _pwc.value && _pw.value !== _pwc.value;
  _hint.hidden = !mismatch;
  _pwc.setCustomValidity(mismatch ? _msg('err_password_mismatch') : '');
}
if (_pw && _pwc) {
  _pw.addEventListener('input', checkPw);
  _pwc.addEventListener('input', checkPw);
}
