/* ZIP preview — shared module (home upload-preview + detail/result overlay).
 *
 * Client-side central-directory parser (no deps):
 *   parseLocalZip(file) — File API slice of the tail + EOCD, ~100 lines.
 * Server-backed listing for uploaded results:
 *   fetchZipList(pid) — GET /f/{pid}/zip-list (Range-based, server-side).
 * UI: showZipModal(entries, meta) — overlay listing names/sizes, capped 500.
 */
(function () {
  "use strict";

  var MAX_CD_BYTES = 512 * 1024;
  var MAX_ENTRIES = 500;
  var EOCD_SIG = 0x06054b50;
  var CD_SIG = 0x02014b50;

  function u16(dv, o) { return dv.getUint16(o, true); }
  function u32(dv, o) { return dv.getUint32(o, true); }

  function findEocd(dv, tailLen) {
    var idx = -1;
    var scanFrom = tailLen - 22;
    var scanTo = Math.max(0, scanFrom - 65536 + 1); // EOCD comment ≤ 64KB
    for (var i = scanFrom; i >= scanTo; i--) {
      if (u32(dv, i) === EOCD_SIG) { idx = i; break; }
    }
    if (idx < 0) return null;
    return {
      total: u16(dv, idx + 10),
      cdSize: u32(dv, idx + 12),
      cdOffset: u32(dv, idx + 16),
    };
  }

  function decodeName(bytes, utf8) {
    try {
      if (utf8) return new TextDecoder("utf-8").decode(bytes);
      return new TextDecoder("utf-8").decode(bytes); // cp437 fallback: utf-8 best effort
    } catch (e) { return ""; }
  }

  /* Parse CD entries from a DataView where CD starts at cdInTail. */
  function parseCd(dv, cdInTail, cdSize, limit) {
    var out = [];
    var pos = cdInTail;
    var end = cdInTail + cdSize;
    while (pos + 46 <= end && out.length < (limit || MAX_ENTRIES)) {
      if (u32(dv, pos) !== CD_SIG) break;
      var flags = u16(dv, pos + 8);
      var csize = u32(dv, pos + 20);
      var usize = u32(dv, pos + 24);
      var nlen = u16(dv, pos + 28);
      var elen = u16(dv, pos + 30);
      var clen = u16(dv, pos + 32);
      var nameBytes = new Uint8Array(dv.buffer, dv.byteOffset + pos + 46, nlen);
      var name = decodeName(nameBytes, !!(flags & 0x800));
      out.push({
        name: name,
        size: usize,
        compressed: csize,
        isDir: name.charAt(name.length - 1) === "/",
        encrypted: !!(flags & 0x1),
      });
      pos += 46 + nlen + elen + clen;
    }
    return out;
  }

  /* Local file preview: read tail slices, resolve names. Rejects on
     unsupported/invalid archives with an i18n-able reason code. */
  function parseLocalZip(file) {
    var size = file.size;
    if (size < 22) return Promise.reject("zip_invalid");
    var tailLen = Math.min(size, MAX_CD_BYTES);
    var tailStart = size - tailLen;
    return file.slice(tailStart, size).arrayBuffer().then(function (buf) {
      var dv = new DataView(buf);
      var eocd = findEocd(dv, tailLen);
      if (!eocd) {
        if (size > MAX_CD_BYTES) return Promise.reject("zip_too_complex");
        return Promise.reject("zip_invalid");
      }
      if (eocd.cdSize === 0xFFFFFFFF || eocd.cdOffset === 0xFFFFFFFF || eocd.total === 0xFFFF) {
        return Promise.reject("zip_unsupported");
      }
      if (eocd.cdSize === 0 || eocd.total === 0) {
        return Promise.reject("zip_empty"); // caller may treat as valid-empty
      }
      var cdInTail = eocd.cdOffset - tailStart;
      if (cdInTail < 0 || cdInTail + eocd.cdSize > tailLen) {
        if (eocd.cdSize > MAX_CD_BYTES) return Promise.reject("zip_too_complex");
        var newStart = Math.max(0, size - eocd.cdSize - 1024);
        return file.slice(newStart, size).arrayBuffer().then(function (b2) {
          var dv2 = new DataView(b2);
          var e2 = findEocd(dv2, b2.byteLength);
          if (!e2) return Promise.reject("zip_invalid");
          return parseCd(dv2, e2.cdOffset - newStart, e2.cdSize);
        });
      }
      return parseCd(dv, cdInTail, eocd.cdSize);
    });
  }

  /* Server listing for an uploaded zip (result-end). */
  function fetchZipList(pid) {
    return fetch("/f/" + encodeURIComponent(pid) + "/zip-list")
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (!d.ok) return Promise.reject(d.error || "zip_invalid");
        return d;
      });
  }

  function esc(s) {
    return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  }

  function fmtSize(n) {
    if (!isFinite(n)) return "";
    if (n < 1024) return n + " B";
    var u = ["KB", "MB", "GB"], i = -1;
    do { n /= 1024; i++; } while (n >= 1024 && i < 2);
    return (n >= 100 ? Math.round(n) : n.toFixed(1)) + " " + u[i];
  }

  /* Overlay UI — reuses .modal pattern; RTL-safe (no directional CSS). */
  function showZipModal(entries, meta) {
    var t = window.t || function (k) { return k; };
    var modal = document.getElementById("zipPreviewModal");
    if (!modal) {
      modal = document.createElement("div");
      modal.id = "zipPreviewModal";
      modal.className = "modal hidden";
      modal.setAttribute("role", "dialog");
      modal.setAttribute("aria-modal", "true");
      modal.innerHTML = '<div class="modal-panel zip-preview-panel">' +
        '<button class="modal-close" type="button" aria-label="Close">' + window.iconSvg("close") + '</button>' +
        '<h2 class="zip-preview-title"></h2>' +
        '<p class="zip-preview-meta"></p>' +
        '<ul class="zip-preview-list"></ul>' +
        "</div>";
      document.body.appendChild(modal);
      modal.querySelector(".modal-close").addEventListener("click", function () { modal.classList.add("hidden"); });
      modal.addEventListener("click", function (e) { if (e.target === modal) modal.classList.add("hidden"); });
    }
    var files = 0, dirs = 0;
    var rows = entries.map(function (e) {
      if (e.isDir) dirs++; else files++;
      return '<li class="zip-item' + (e.isDir ? " dir" : "") + '">' +
        (e.isDir ? '<span class="zip-item-icon">▤</span>' : '<span class="zip-item-icon">▪</span>') +
        '<span class="zip-item-name">' + esc(e.name) + "</span>" +
        '<span class="zip-item-size">' + fmtSize(e.size) + "</span></li>";
    }).join("");
    modal.querySelector(".zip-preview-title").textContent = t("zip_preview_title");
    var metaText = t("zip_preview_meta", { files: files, dirs: dirs });
    if (meta && meta.truncated) metaText += " · " + t("zip_preview_truncated");
    modal.querySelector(".zip-preview-meta").textContent = metaText;
    modal.querySelector(".zip-preview-list").innerHTML = rows;
    modal.classList.remove("hidden");
  }

  function showZipError(reason) {
    var t = window.t || function (k) { return k; };
    var keyMap = {
      zip_invalid: "zip_err_invalid",
      zip_unsupported: "zip_err_unsupported",
      zip_too_complex: "zip_err_too_complex",
      zip_empty: "zip_err_empty",
    };
    var key = keyMap[reason] || "zip_err_invalid";
    showZipModal([], { error: true });
    var modal = document.getElementById("zipPreviewModal");
    modal.querySelector(".zip-preview-list").innerHTML =
      '<li class="zip-item"><span class="zip-item-name">' + esc(t(key)) + "</span></li>";
  }

  /* Wire a "preview zip" button for a local File (before upload). */
  function wireLocalPreview(btn, getFile) {
    btn.addEventListener("click", function () {
      var f = getFile();
      if (!f) return;
      parseLocalZip(f).then(
        function (entries) { showZipModal(entries, { truncated: entries.length >= MAX_ENTRIES }); },
        showZipError
      );
    });
  }

  /* Wire a "preview zip" button for an uploaded file (result/detail). */
  function wireRemotePreview(btn, pid) {
    btn.addEventListener("click", function () {
      fetchZipList(pid).then(
        function (d) { showZipModal(d.entries, d); },
        showZipError
      );
    });
  }

  window.zipPreview = {
    parseLocalZip: parseLocalZip,
    fetchZipList: fetchZipList,
    showZipModal: showZipModal,
    showZipError: showZipError,
    wireLocalPreview: wireLocalPreview,
    wireRemotePreview: wireRemotePreview,
    isZipName: function (name, type) {
      if (type === "application/zip" || type === "application/x-zip-compressed") return true;
      return /\.zip$/i.test(String(name || ""));
    },
  };
})();
