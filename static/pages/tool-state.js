// Presentation-only projection for canonical tool params on the home selector.
// Tool Contract stays unchanged: this reads Catalog + committed local UI state
// and renders one compact primary enum value beside the active tool name.
(function () {
  "use strict";

  const home = document.getElementById("homeTools");
  if (!home) return;

  const tools = () => Array.isArray(window.__TOOLS__) ? window.__TOOLS__ : [];
  const resolve = (value) => {
    if (typeof window.resolveLabel === "function") return window.resolveLabel(value);
    if (typeof value === "string") return value;
    if (!value || typeof value !== "object") return "";
    const lang = window.__LANG || "en";
    return value[lang] || value.en || Object.values(value).find(Boolean) || "";
  };

  function savedParams(toolId) {
    try {
      const all = JSON.parse(localStorage.getItem("flashshare_tool_params") || "{}");
      const value = all && all[toolId];
      return value && typeof value === "object" ? value : {};
    } catch (e) {
      return {};
    }
  }

  function primaryEnumState(tool) {
    if (!tool || !Array.isArray(tool.params)) return "";
    const saved = savedParams(tool.id);
    for (const param of tool.params) {
      if (!param || param.type !== "enum" || !Array.isArray(param.options)) continue;
      const value = saved[param.key] !== undefined ? saved[param.key] : param.default;
      const option = param.options.find(o => o && o.value === value);
      if (!option) continue;
      return resolve(option.label) || String(value || "");
    }
    return "";
  }

  function render() {
    const active = home.querySelector(".tool-tab.active");
    home.querySelectorAll(".tool-param-state").forEach(el => {
      if (!active || el.parentElement !== active) el.remove();
    });
    if (!active) return;

    const tool = tools().find(t => t.id === active.dataset.toolId);
    const state = primaryEnumState(tool);
    let badge = active.querySelector(".tool-param-state");
    if (!state) {
      if (badge) badge.remove();
      return;
    }

    const text = " · " + state;
    if (!badge) {
      badge = document.createElement("span");
      // Reuse the existing preset-badge visual grammar; this class is only
      // presentation. tool-param-state distinguishes canonical params.
      badge.className = "tool-preset-badge tool-param-state";
      active.appendChild(badge);
    }
    if (badge.textContent !== text) badge.textContent = text;
  }

  let queued = false;
  const schedule = () => {
    if (queued) return;
    queued = true;
    queueMicrotask(() => {
      queued = false;
      render();
    });
  };

  new MutationObserver(schedule).observe(home, {
    subtree: true,
    childList: true,
    attributes: true,
    attributeFilter: ["class"],
  });

  // Confirm commits params/localStorage in app.js before this event bubbles to
  // document, so a zero-delay projection sees the canonical committed state.
  document.addEventListener("click", (event) => {
    if (event.target.closest("#overlayConfirm")) setTimeout(render, 0);
  });

  window.ToolPresentation = Object.assign(window.ToolPresentation || {}, {
    primaryEnumState,
    renderHomeState: render,
  });
  render();
})();
