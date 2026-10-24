// ── auth-modal: 全站登录/注册浮层 ──────────────────────────────
// window.openAuthModal({mode, onSuccess, onClose}) — 提交走 POST
// /auth/login|register（Accept: application/json, 同一限流/会话路径），
// 成功后关闭浮层、更新 window.__USER__、页头登录/注册按钮就地换成
// 头像（/account/credits），再调用 onSuccess。取消（Esc/遮罩/关闭钮）
// 调用 onClose。X 一键登录是整页跳转（已选文件会丢，报告已注明）。
// 表单内容与 /auth/login|register 独立页同源：克隆 base.html 服务端
// 渲染的 <template id="authTpl*">（auth/_card.html 单源），本文件只
// 负责容器（遮罩/关闭/焦点圈）与提交/切换行为，不再手写字段。
(function () {
  "use strict";

  const t = (k) => window.t(k);
  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g,
    (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  let overlay = null;
  let lastFocus = null;
  let current = null; // {onSuccess, onClose, settled}

  // 密码不一致提示（登录页注册页/浮层共用）：限定在传入表单内查询，
  // 页面表单与浮层克隆同一行为、同一份文案。
  window.bindPwMismatch = function (form) {
    if (!form) return;
    const pw = form.querySelector('input[name="password"]');
    const pwc = form.querySelector('input[name="password_confirm"]');
    const hint = form.querySelector(".pw-mismatch");
    if (!pw || !pwc || !hint) return;
    const check = () => {
      const mismatch = pwc.value && pw.value !== pwc.value;
      hint.hidden = !mismatch;
      pwc.setCustomValidity(mismatch
        ? (window._msg ? window._msg("err_password_mismatch") : "x")
        : "");
    };
    pw.addEventListener("input", check);
    pwc.addEventListener("input", check);
    form.addEventListener("submit", (e) => {
      if (pw.value !== pwc.value) { e.preventDefault(); hint.hidden = false; }
    });
  };

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

  // 当前语言应用到克隆子树 (SSR 语言可能已被站内就地切换超越): 与
  // 引擎 applyI18n 同一替换规则, 只作用于浮层子树 — 不触发整页重绘
  function applyLang(root) {
    root.querySelectorAll("[data-i18n]").forEach((el) => {
      try { el.textContent = window.t(el.getAttribute("data-i18n")); }
      catch (err) { /* keep SSR text */ }
    });
    root.querySelectorAll("[data-i18n-placeholder]").forEach((el) => {
      try { el.setAttribute("placeholder",
        window.t(el.getAttribute("data-i18n-placeholder"))); }
      catch (err) { /* keep SSR placeholder */ }
    });
  }

  // 聚焦表单首个可见可用控件 (排除 hidden 字段 — 克隆的 next 隐藏域
  // 是 DOM 首个 input, 老 querySelector("input") 会拿到它导致零聚焦)
  function focusFirstControl(form) {
    if (!form) return;
    const first = form.querySelector(
      "input:not([type=hidden]):not([disabled]),"
      + " select:not([disabled]), textarea:not([disabled])");
    if (first) first.focus();
  }

  function fillBody(mode) {
    const tpl = document.getElementById(
      mode === "register" ? "authTplRegister" : "authTplLogin");
    if (!tpl) return null;
    const body = overlay.querySelector(".am-body");
    body.innerHTML = "";
    body.appendChild(tpl.content.cloneNode(true));
    // X 一键登录是整页跳转：服务端模板不知道运行时所在页，挂载时把
    // 当前地址写进 next
    const here = location.pathname + location.search;
    body.querySelectorAll("a[data-nextfix]").forEach((a) => {
      a.href = "/auth/x/login?next=" + encodeURIComponent(here);
    });
    applyLang(body);
    overlay.setAttribute("aria-label", body.querySelector(".auth-title")
      ? body.querySelector(".auth-title").textContent : "");
    const form = body.querySelector("form");
    window.bindPwMismatch(form);
    return form;
  }

  // 提交处理 (对当前挂载的表单): 阻止默认导航, 按表单 action 读取模式
  function handleSubmit(form) {
    const errEl = overlay.querySelector(".am-err");
    errEl.hidden = true;
    const action = form.getAttribute("action") || "/auth/login";
    const mode2 = /\/register$/.test(action) ? "register" : "login";
    // 密码不一致: bindPwMismatch 的表单级监听先 preventDefault, 但不
    // 拦冒泡 — 委托层同判一次, 不放行 fetch
    const pw = form.querySelector('input[name="password"]');
    const pwc = form.querySelector('input[name="password_confirm"]');
    const hint = form.querySelector(".pw-mismatch");
    if (pw && pwc && hint && pw.value !== pwc.value) {
      hint.hidden = false;
      pwc.focus();
      return;
    }
    const fd = new FormData(form);
    fd.set("next", location.pathname + location.search);
    const btn = form.querySelector('button[type="submit"]');
    btn.disabled = true;
    fetch(action, {
      method: "POST",
      headers: { Accept: "application/json" },
      body: fd,
    }).then((r) => r.json().catch(() => null)).then((d) => {
      if (d && d.ok) {
        window.__USER__ = { username: d.user.username, displayName: d.user.display_name };
        swapHeaderToAvatar(d.user);
        close("success");
        return;
      }
      errEl.textContent = d && d.error ? t(d.error) : t("err_unexpected");
      errEl.hidden = false;
    }).catch(() => {
      errEl.textContent = t("err_unexpected");
      errEl.hidden = false;
    }).finally(() => { btn.disabled = false; });
  }

  function render(mode) {
    overlay = document.createElement("div");
    overlay.className = "modal";
    overlay.setAttribute("role", "dialog");
    overlay.setAttribute("aria-modal", "true");
    overlay.innerHTML =
      '<div class="modal-panel auth-modal">'
      + '<button class="modal-close" type="button" aria-label="' + esc(t("close") || "Close") + '">'
      + window.iconSvg("close") + "</button>"
      + '<div class="am-body"></div>'
      + "</div>";
    document.body.appendChild(overlay);
    overlay.addEventListener("click", (e) => { if (e.target === overlay) close("cancel"); });
    overlay.querySelector(".modal-close").addEventListener("click", () => close("cancel"));
    const form = fillBody(mode);
    if (!form) { close("cancel"); return; }
    // 事件全部挂在稳定容器 (.am-body) 上, 委托到当前挂载的表单 —
    // 模式切换换克隆后监听不失效 (旧实现把 submit 绑在首个克隆上,
    // 切换后的表单无人处理)
    const body = overlay.querySelector(".am-body");
    body.addEventListener("click", (e) => {
      const seg = e.target.closest(".seg [data-mode]");
      if (!seg) return;
      const f = fillBody(seg.dataset.mode);
      if (f) focusFirstControl(f);
    });
    body.addEventListener("submit", (e) => {
      const f = e.target.closest("form");
      if (!f) return;
      e.preventDefault();
      handleSubmit(f);
    });
    focusFirstControl(form);
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
