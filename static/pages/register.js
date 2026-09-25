const _pw = document.getElementById('regPassword');
const _pwc = document.getElementById('regPasswordConfirm');
const _hint = document.getElementById('pwMismatch');
const _form = document.getElementById('registerForm');
function checkPw() {
  if (_pwc.value && _pw.value !== _pwc.value) {
    _hint.hidden = false;
    _pwc.setCustomValidity(window._msg ? _msg('err_password_mismatch') : 'x');
  } else {
    _hint.hidden = true;
    _pwc.setCustomValidity('');
  }
}
_pw.addEventListener('input', checkPw);
_pwc.addEventListener('input', checkPw);
_form.addEventListener('submit', function(e) {
  if (_pw.value !== _pwc.value) { e.preventDefault(); _hint.hidden = false; }
});
