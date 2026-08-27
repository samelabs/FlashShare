// PWA registration (loaded on home). Cache-name bump in sw.js triggers update;
// updateViaCache:"none" keeps the SW itself out of HTTP-cache traps.
if ("serviceWorker" in navigator) {
  window.addEventListener("load", () =>
    navigator.serviceWorker.register("/sw.js", { updateViaCache: "none" }).catch(() => {}));
}
