// /f/{id}/print — PDF print view.
// Print strategy: mobile browsers cannot paginate embedded-PDF print()
// (they print only the first page). Render every page to a canvas with
// PDF.js and print the host document instead — pagination is then plain
// CSS page breaks and works everywhere. The <iframe> stays as a visual
// fallback for browsers where PDF.js fails to load; its src is the PDF
// URL this script renders.
(async function () {
  var btn = document.getElementById("printBtn");
  var box = document.getElementById("pages");
  var statusEl = document.getElementById("status");
  var frame = document.getElementById("pdfFrame");

  function ready() {
    btn.disabled = false;
    statusEl.style.display = "none";
  }
  function fail() {
    // PDF.js unavailable → fall back to iframe print (desktop-OK) and
    // keep the download hint visible.
    btn.disabled = false;
    var st = document.getElementById("statusText");
    if (st) st.textContent = "";
    statusEl.style.display = "none";
  }

  try {
    const { GlobalWorkerOptions, getDocument } = await import("/static/pdfjs/pdf.min.mjs");
    GlobalWorkerOptions.workerSrc = "/static/pdfjs/pdf.worker.min.mjs";

    const task = getDocument({ url: frame.getAttribute("src"), isEvalSupported: false });
    const pdf = await task.promise;
    const n = pdf.numPages;

    const DPR = Math.min(window.devicePixelRatio || 1, 2);
    // ~150 DPI: enough for print legibility without multi-hundred-MB canvases.
    const SCALE = 150 / 72 * DPR > 2 ? 2 : (150 / 72) * DPR;
    // Reserve one slot per page in page order BEFORE any render completes.
    // Rendering runs in parallel and finishes out of order — appending
    // canvases directly would make DOM order = race order (pages printed
    // scrambled). Slots pin the order; each job fills its own slot.
    const slots = [];
    for (let i = 1; i <= n; i++) {
      const slot = document.createElement("div");
      slot.className = "page-slot";
      slot.dataset.page = String(i);
      box.appendChild(slot);
      slots.push(slot);
    }
    const jobs = [];
    for (let i = 1; i <= n; i++) {
      jobs.push(pdf.getPage(i).then(page => {
        const vp = page.getViewport({ scale: SCALE });
        const canvas = document.createElement("canvas");
        canvas.dataset.page = String(i);
        canvas.width = Math.floor(vp.width);
        canvas.height = Math.floor(vp.height);
        return page.render({ canvasContext: canvas.getContext("2d", { alpha: false }), viewport: vp }).promise.then(() => {
          slots[i - 1].appendChild(canvas);
        });
      }));
    }
    await Promise.all(jobs);

    if (n > 0) {
      box.style.display = "flex";
      frame.style.display = "none";
    }
    ready();
    btn.addEventListener("click", function () { window.print(); });
  } catch (e) {
    fail();
    btn.addEventListener("click", function () {
      try {
        frame.contentWindow.focus();
        frame.contentWindow.print();
      } catch (err) { window.print(); }
    });
  }
})();
