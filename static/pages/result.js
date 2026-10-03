// ── Result page (/r/{id}) enhancements ─────────────────────────
// The page is server-rendered readable (text/JSON shown, files linked
// — no JS required). This file wires the progressive actions only:
// copy text / copy JSON / download all.
// Result expiry is enforced server-side (expired → 404), so every
// element this script touches only exists on a live result.
(function () {
  "use strict";

  const t = (k, p) => window.t(k, p);

  function flash(btn, icon) {
    const orig = btn.innerHTML;
    btn.innerHTML = window.iconSvg(icon);
    btn.style.color = "var(--success)";
    setTimeout(() => {
      btn.innerHTML = orig;
      btn.style.color = "";
    }, 1400);
  }

  async function copyText(value, btn) {
    try {
      await navigator.clipboard.writeText(value);
      flash(btn, "check");
    } catch (e) { /* clipboard blocked — SSR content stays selectable */ }
  }

  window.linkifyResultText && window.linkifyResultText(document);

  // Per-item copy: text items copy their text, JSON items copy the
  // pretty-printed JSON exactly as rendered.
  document.querySelectorAll("[data-copy-text], [data-copy-json]").forEach((btn) => {
    const item = btn.closest(".rs-item");
    const pre = item ? item.querySelector(".rs-text, .rs-json") : null;
    btn.addEventListener("click", () => copyText(pre ? pre.textContent : "", btn));
  });

  // Copy link / system share / X live in the share bar (base-engine.js).

  // Download all — client-side only (file bytes never pass through the
  // 518 server; each file is fetched through its own download=1 link,
  // so every file spends one download exactly like a manual click):
  //   desktop → one store-only .zip (images/PDFs are already compressed)
  //   touch devices with Web Share files → files handed to the system
  //     share sheet ("save N images" to the photo library); share needs
  //     a fresh tap, so the first tap prepares and the second shares
  //   over the size caps, a fetch failure, or no support → the plain
  //     sequential per-file downloads.
  const ZIP_MAX = 500 * 1024 * 1024;
  const SHARE_MAX = 100 * 1024 * 1024;
  const allBtn = document.getElementById("rDownloadAll");
  if (allBtn) {
    // Installed-PWA mode hides the bundle action: opening one secondary
    // context per file gets popup-blocked; per-file downloads remain.
    if (window.isStandalonePWA && window.isStandalonePWA()) {
      allBtn.style.display = "none";
    }
    // Both download shapes: plain anchors AND the PWA-rebound buttons
    // (wirePwaDownloadLinks strips their href).
    const links = () => Array.from(document.querySelectorAll('.rs-item .rs-dl'));
    const sequential = () => links().forEach((a, i) => setTimeout(() => a.click(), i * 400));
    const label = allBtn.querySelector("span");
    const origLabel = label ? label.textContent : "";
    const setLabel = (txt) => { if (label) label.textContent = txt; };
    const touch = !!(window.matchMedia && window.matchMedia("(pointer: coarse)").matches);
    let ready = null;   // prepared File[] waiting for the share tap
    let busy = false;

    // Fetches in order; stops at the first failure and reports how far
    // it got (a fetched file already spent its download — the fallback
    // must not fetch it twice).
    async function fetchAll(entries) {
      const got = [];
      for (let i = 0; i < entries.length; i++) {
        setLabel(t("r_preparing", { n: i + 1, total: entries.length }));
        try {
          const r = await fetch(entries[i].url, { credentials: "same-origin" });
          // an HTML answer for a non-HTML file is an error page (expired
          // / exhausted link), never file content
          const ct = r.headers.get("content-type") || "";
          if (!r.ok || (/text\/html/i.test(ct) && !/\.html?$/i.test(entries[i].name))) {
            return { got: got, failedAt: i };
          }
          got.push({ name: entries[i].name, blob: await r.blob() });
        } catch (e) { return { got: got, failedAt: i }; }
      }
      return { got: got, failedAt: -1 };
    }
    function saveBlob(blob, name) {
      const a = document.createElement("a");
      a.href = URL.createObjectURL(blob);
      a.download = name;
      document.body.appendChild(a);
      a.click();
      a.remove();
      setTimeout(() => URL.revokeObjectURL(a.href), 60000);
    }

    allBtn.addEventListener("click", async () => {
      if (busy) return;
      if (ready) {               // second tap on touch: system share sheet
        const files = ready;
        ready = null;
        setLabel(origLabel);
        try { await navigator.share({ files: files }); } catch (e) { /* cancel is a normal end */ }
        return;
      }
      const entries = links().map((a) => ({
        url: a.getAttribute("href") || a.dataset.dlUrl || "",
        name: a.dataset.name || "file",
        size: parseInt(a.dataset.size || "0", 10) || 0,
      })).filter((e) => e.url);
      const total = entries.reduce((n, e) => n + e.size, 0);
      const canFiles = touch && navigator.canShare && navigator.share;
      if (!entries.length || total > (canFiles ? SHARE_MAX : ZIP_MAX)) { sequential(); return; }
      busy = true;
      allBtn.disabled = true;
      try {
        const res = await fetchAll(entries);
        if (res.failedAt >= 0) {
          // hand over what was fetched, download the rest one by one
          res.got.forEach((g, i) => setTimeout(() => saveBlob(g.blob, g.name), i * 400));
          const rest = links().slice(res.failedAt);
          rest.forEach((a, i) => setTimeout(() => a.click(), (res.got.length + i) * 400));
          setLabel(origLabel);
          return;
        }
        let files = null;
        if (canFiles) {
          files = res.got.map((g) => new File([g.blob], g.name,
            { type: g.blob.type || "application/octet-stream" }));
          if (!navigator.canShare({ files: files })) files = null;
        }
        if (files) {
          ready = files;
          setLabel(t("r_save_all"));
        } else {
          const title = ((document.querySelector(".rc-top h1") || {}).textContent || "518plus").trim();
          saveBlob(await window.zipStore(res.got),
            title.replace(/[\\/:*?"<>|]+/g, "_").slice(0, 80) + ".zip");
          setLabel(origLabel);
        }
      } finally {
        busy = false;
        allBtn.disabled = false;
      }
    });
  }

  // Standalone PWA: download anchors route through the engine authority
  // (common.js wirePwaDownloadLinks — no in-window navigation).
  window.wirePwaDownloadLinks && window.wirePwaDownloadLinks(document);
  // Image grid cells open the full-size view (same lightbox as the
  // inline result card — common.js single authority).
  window.wireImageZoom && window.wireImageZoom(document);
})();
