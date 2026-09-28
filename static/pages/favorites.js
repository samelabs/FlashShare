// 我的收藏: toggle a tool in/out of the user's favorites.
// Shared by the tool page star (#favBtn) and the account favorites list.
(function () {
  "use strict";

  function setFavorite(toolKey, on) {
    return fetch("/api/favorites", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ tool_key: toolKey, on: on }),
    }).then((r) => r.json());
  }
  window.setFavorite = setFavorite;

  // tool page star
  const btn = document.getElementById("favBtn");
  if (btn) {
    btn.addEventListener("click", () => {
      if (window.__USER__) { toggle(); return; }
      if (typeof window.openAuthModal === "function") {
        window.openAuthModal({ mode: "login", onSuccess: toggle });
      } else {
        location.href = "/auth/login?next=" + encodeURIComponent(location.pathname);
      }
    });
    async function toggle() {
      const next = btn.getAttribute("aria-pressed") !== "true";
      btn.disabled = true;
      try {
        const d = await setFavorite(btn.dataset.tool, next);
        if (d && d.ok) {
          btn.setAttribute("aria-pressed", next ? "true" : "false");
          btn.classList.toggle("on", next);
          const label = btn.querySelector("span");
          if (label) {
            const key = next ? "fav_on" : "fav_add";
            label.setAttribute("data-i18n", key);
            label.textContent = window.t(key);
          }
        }
      } finally {
        btn.disabled = false;
      }
    }
  }

  // account favorites list
  const list = document.getElementById("favList");
  if (list) {
    list.addEventListener("click", async (e) => {
      const b = e.target.closest(".fav-remove");
      if (!b) return;
      b.disabled = true;
      const d = await setFavorite(b.dataset.tool, false).catch(() => null);
      if (d && d.ok) {
        const item = b.closest(".fav-item");
        if (item) item.remove();
        if (!list.querySelector(".fav-item")) {
          list.remove();
          const empty = document.getElementById("favEmpty");
          if (empty) empty.hidden = false;
        }
      } else {
        b.disabled = false;
      }
    });
  }
})();
