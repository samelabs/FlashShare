// ── auth-modal: 全站登录/注册浮层 ──────────────────────────────
// window.openAuthModal({mode, onSuccess, onClose}) — 提交走 POST
// /auth/login|register（Accept: application/json, 同一限流/会话路径），
// 成功后关闭浮层、更新 window.__USER__、页头登录/注册按钮就地换成
// 头像（/account/credits），再调用 onSuccess。取消（Esc/遮罩/关闭钮）
// 调用 onClose。X 一键登录是整页跳转（已选文件会丢，报告已注明）。
(function () {
  "use strict";

  const t = (k) => window.t(k);
  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g,
    (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  let overlay = null;
  let lastFocus = null;
  let current = null; // {onSuccess, onClose, settled}

  function close(settled) {
    if (!overlay) return;
    const cur = current;
    overlay.remove();
    overlay = null;
    document.removeEventListener("keydown", onKey, true);
    if (lastFocus && lastFocus.focus) lastFocus.focus();
    lastFocus = null;
    current = null;
    if (cur && !cur.settled) {
      cur.settled = true;
      if (settled === "success" && typeof cur.onSuccess === "function") cur.onSuccess();
      else if (typeof cur.onClose === "function") cur.onClose();
    }
  }

  function onKey(e) {
    if (e.key === "Escape") { e.preventDefault(); close("cancel"); return; }
    if (e.key === "Tab" && overlay) {
      // focus lock inside the modal
      const focusables = overlay.querySelectorAll(
        'a[href], button:not([disabled]), input:not([disabled]), [tabindex]:not([tabindex="-1"])');
      if (!focusables.length) return;
      const first = focusables[0], last = focusables[focusables.length - 1];
      if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
      else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
    }
  }

  function swapHeaderToAvatar(user) {
    const bar = document.querySelector(".hd-r");
    if (!bar) return;
    bar.querySelectorAll('a[href="/auth/login"], a[href="/auth/register"]').forEach((a) => a.remove());
    if (bar.querySelector(".av")) return;
    const av = document.createElement("a");
    av.className = "av";
    av.href = "/account/credits";
    av.setAttribute("aria-label", (user && user.display_name) || "");
    av.textContent = ((user && (user.display_name || user.username)) || "?").slice(0, 1).toUpperCase();
    const menu = bar.querySelector(".menu");
    bar.insertBefore(av, menu || null);
  }

  function formHtml(mode) {
    if (mode === "register") {
      return (
        '<label class="field"><span class="field-label">' + esc(t("usernameHint")) + "</span>"
        + '<input class="input" type="text" name="username" required autocomplete="username" minlength="5" maxlength="32" pattern="[A-Za-z0-9_\\-]{5,32}"></label>'
        + '<label class="field"><span class="field-label">' + esc(t("emailLabel")) + "</span>"
        + '<input class="input" type="email" name="email" required autocomplete="email" maxlength="254"></label>'
        + '<label class="field"><span class="field-label">' + esc(t("newPassword")) + "</span>"
        + '<input class="input" type="password" name="password" required autocomplete="new-password" minlength="8" maxlength="128"></label>'
        + '<label class="field"><span class="field-label">' + esc(t("confirmPasswordLabel")) + "</span>"
        + '<input class="input" type="password" name="password_confirm" required autocomplete="new-password" minlength="8" maxlength="128"></label>'
        + '<button type="submit" class="btn btn-p btn-lg">' + esc(t("signUp")) + "</button>");
    }
    return (
      '<label class="field"><span class="field-label">' + esc(t("loginLabel")) + "</span>"
      + '<input class="input" type="text" name="login" required autocomplete="username" maxlength="254"></label>'
      + '<label class="field"><span class="field-label">' + esc(t("passwordLabel")) + "</span>"
      + '<input class="input" type="password" name="password" required autocomplete="current-password" maxlength="128"></label>'
      + '<button type="submit" class="btn btn-p btn-lg">' + esc(t("signIn")) + "</button>");
  }

  function render(mode) {
    const x = !!window.__X_LOGIN__;
    const next = location.pathname + location.search;
    overlay = document.createElement("div");
    overlay.className = "modal";
    overlay.setAttribute("role", "dialog");
    overlay.setAttribute("aria-modal", "true");
    overlay.setAttribute("aria-label", mode === "register" ? t("registerTitle") : t("loginTitle"));
    overlay.innerHTML =
      '<div class="modal-panel auth-modal">'
      + '<button class="modal-close" type="button" aria-label="' + esc(t("close") || "Close") + '">'
      + window.iconSvg("close") + "</button>"
      + "<h2>" + esc(mode === "register" ? t("registerTitle") : t("loginTitle")) + "</h2>"
      + '<p class="auth-sub">' + esc(t("auth_modal_sub")) + "</p>"
      + '<div class="seg" role="tablist">'
      + '<button type="button" role="tab" data-mode="login"' + (mode === "login" ? ' class="on"' : "") + ">" + esc(t("signIn")) + "</button>"
      + '<button type="button" role="tab" data-mode="register"' + (mode === "register" ? ' class="on"' : "") + ">" + esc(t("signUp")) + "</button>"
      + "</div>"
      + (x
        ? '<div class="auth-x-wrap"><a class="x-login" href="/auth/x/login?next=' + encodeURIComponent(next) + '">'
          + '<span class="x-glyph" aria-hidden="true">𝕏</span><span>' + esc(t("login")) + "</span></a></div>"
          + '<div class="auth-divider"><span>' + esc(t("auth_or")) + "</span></div>"
        : "")
      + '<p class="error-note am-err" hidden></p>'
      + '<form novalidate>' + formHtml(mode) + "</form>"
      + '<p class="auth-terms"><a href="/terms">' + esc(t("footer_terms")) + '</a> · <a href="/privacy">' + esc(t("footer_privacy")) + "</a></p>"
      + "</div>";
    document.body.appendChild(overlay);
    overlay.addEventListener("click", (e) => { if (e.target === overlay) close("cancel"); });
    overlay.querySelector(".modal-close").addEventListener("click", () => close("cancel"));
    overlay.querySelectorAll(".seg [data-mode]").forEach((b) => {
      b.addEventListener("click", () => {
        const m = b.dataset.mode;
        overlay.querySelector("form").innerHTML = formHtml(m);
        // the title is a function of the CURRENT mode — heading and the
        // overlay's aria-label follow the tab exactly like the form does
        const title = t(m === "register" ? "registerTitle" : "loginTitle");
        overlay.querySelector("h2").textContent = title;
        overlay.setAttribute("aria-label", title);
        overlay.querySelectorAll(".seg [data-mode]").forEach((x2) => {
          x2.classList.toggle("on", x2 === b);
        });
        overlay.querySelector("form input") && overlay.querySelector("form input").focus();
      });
    });
    const form = overlay.querySelector("form");
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const errEl = overlay.querySelector(".am-err");
      errEl.hidden = true;
      const mode2 = overlay.querySelector(".seg .on").dataset.mode;
      const fd = new FormData(form);
      fd.set("next", location.pathname + location.search);
      const btn = form.querySelector('button[type="submit"]');
      btn.disabled = true;
      try {
        const r = await fetch("/auth/" + (mode2 === "register" ? "register" : "login"), {
          method: "POST",
          headers: { Accept: "application/json" },
          body: fd,
        });
        const d = await r.json().catch(() => null);
        if (d && d.ok) {
          window.__USER__ = { username: d.user.username, displayName: d.user.display_name };
          swapHeaderToAvatar(d.user);
          close("success");
          return;
        }
        errEl.textContent = d && d.error ? t(d.error) : t("err_unexpected");
        errEl.hidden = false;
      } catch (err2) {
        errEl.textContent = t("err_unexpected");
        errEl.hidden = false;
      } finally {
        btn.disabled = false;
      }
    });
    const firstInput = overlay.querySelector("input");
    if (firstInput) firstInput.focus();
  }

  window.openAuthModal = function (opts) {
    opts = opts || {};
    if (overlay) close("cancel");
    lastFocus = document.activeElement;
    current = { onSuccess: opts.onSuccess, onClose: opts.onClose, settled: false };
    document.addEventListener("keydown", onKey, true);
    render(opts.mode === "register" ? "register" : "login");
  };

  // header buttons open the modal (href stays as the no-JS fallback)
  document.addEventListener("DOMContentLoaded", () => {
    document.querySelectorAll('a[href="/auth/login"].js-auth, a[href="/auth/register"].js-auth').forEach((a) => {
      a.addEventListener("click", (e) => {
        e.preventDefault();
        window.openAuthModal({ mode: a.getAttribute("href").endsWith("register") ? "register" : "login" });
      });
    });
  });
})();
