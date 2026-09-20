#!/usr/bin/env node
/* Web-flow JS behavior contract: real process-flow.js execution.
 *
 * Loads the ACTUAL static/pages/process-flow.js in a vm sandbox with
 * mocked XHR/fetch/DOM. runToolFlow() drives the real upload+execute
 * code path; we capture the outgoing requests and assert the operation
 * identity contract:
 *   - upload FormData carries a non-empty client_ref
 *   - execute JSON carries a non-empty client_ref
 *   - both are STRICTLY IDENTICAL (one mint per run)
 *   - execute never sends null
 *   - app.js newOperationRef is the only generator (process-flow must
 *     throw without it — no second UUID implementation inside)
 */
"use strict";
const fs = require("fs");
const path = require("path");
const vm = require("vm");
const assert = require("assert");

const REPO = path.resolve(__dirname, "..", "..");
const sandboxBase = {
  console,
  setTimeout: (fn) => fn(),
  location: { href: "https://ci.test/" },
  URL, URLSearchParams, TextEncoder, TextDecoder,
  __USER__: { id: 1, name: "ci" },
  __LANG: "en",
};

function makeSandbox() {
  const sandbox = vm.createContext(Object.assign({}, sandboxBase));
  return sandbox;
}

// ── minimal DOM shims the script touches at load/run time ──
function installDOM(sandbox) {
  const noop = () => {};
  const fakeElement = () => ({
    addEventListener: noop, removeEventListener: noop,
    classList: { add: noop, remove: noop, toggle: noop },
    style: {}, textContent: "", innerHTML: "", appendChild: noop,
    setAttribute: noop, getAttribute: () => null, querySelector: () => null,
  });
  sandbox.document = {
    getElementById: () => null,
    querySelector: () => null,
    querySelectorAll: () => [],
    createElement: fakeElement,
    addEventListener: noop,
    documentElement: fakeElement(),
    head: fakeElement(), body: fakeElement(),
    cookie: "",
  };
  sandbox.window = sandbox;
}

class FormDataDouble {
  constructor() { this._fields = []; this._files = []; }
  append(k, v) {
    if (v && typeof v === "object" && typeof v.name === "string") this._files.push([k, v]);
    else this._fields.push([k, String(v)]);
  }
  get(k) {
    const hit = this._fields.find(([kk]) => kk === k);
    return hit ? hit[1] : null;
  }
  has(k) { return this._fields.some(([kk]) => kk === k) || this._files.some(([kk]) => kk === k); }
}

function run(capture, refMinted) {
  const sandbox = makeSandbox();
  installDOM(sandbox);
  sandbox.__mints = 0;
  // app.js generator, exposed the same way app.js exposes it — the ONLY
  // generator. Count mints to prove one-per-run.
  sandbox.crypto = {
    getRandomValues: (arr) => { for (let i = 0; i < arr.length; i++) arr[i] = i + 1; return arr; },
    randomUUID: () => "11111111-2222-3333-4444-555555555555",
  };
  sandbox.newOperationRef = function () {
    sandbox.__mints++;
    return "web:test-ref-" + Math.random().toString(36).slice(2);
  };
  sandbox.FormData = FormDataDouble;
  sandbox.XMLHttpRequest = class {
    open(m, u) { this._url = u; }
    send(fd) {
      capture.upload = { url: this._url, form: fd };
      this.status = 200;
      this.responseText = JSON.stringify({
        ok: true,
        file: { publicId: "fCI1", sourceToken: "tok1" },
      });
      Promise.resolve().then(() => this.onload && this.onload());
    }
    setRequestHeader() {}
    get upload() { return this._upload || (this._upload = {}); }
  };
  sandbox.fetch = async (url, init) => {
    capture.execute = { url, body: JSON.parse(init.body) };
    return { json: async () => ({ ok: true, result: { publicId: "r1" }, balance: 1 }) };
  };
  const src = fs.readFileSync(path.join(REPO, "static", "pages", "process-flow.js"), "utf8");
  vm.runInContext(src, sandbox, { filename: "process-flow.js" });
  return sandbox;
}

(async () => {
  let capture = {};
  const sandbox = run(capture);
  assert.strictEqual(typeof sandbox.runToolFlow, "function", "runToolFlow exported");

  const file = { name: "a.png", size: 8 };
  await sandbox.runToolFlow(
    { id: "compress", label: "C", params: [], multi: false, pass_through: false },
    [file], {});

  // upload FormData client_ref
  assert.ok(capture.upload, "upload request captured");
  assert.strictEqual(capture.upload.url, "/api/uploads");
  const uploadRef = capture.upload.form.get("client_ref");
  assert.ok(typeof uploadRef === "string" && uploadRef.length > 0,
    "upload client_ref non-empty");
  assert.ok(uploadRef.startsWith("web:"), "ref minted by app.js generator");

  // execute JSON client_ref — identical, never null
  assert.ok(capture.execute, "execute request captured");
  assert.strictEqual(capture.execute.url, "/api/tools/execute");
  const execRef = capture.execute.body.client_ref;
  assert.ok(typeof execRef === "string" && execRef.length > 0,
    "execute client_ref non-empty");
  assert.notStrictEqual(execRef, null);
  assert.strictEqual(uploadRef, execRef, "upload ref === execute ref (same run)");

  // ONE mint per run
  assert.strictEqual(sandbox.__mints, 1, "exactly one ref mint per run");

  // second run mints a fresh identity (no reuse across runs)
  let capture2 = {};
  const s2 = run(capture2);
  await s2.runToolFlow(
    { id: "compress", label: "C", params: [], multi: false, pass_through: false },
    [file], {});
  const r2a = capture2.upload.form.get("client_ref");
  const r2b = capture2.execute.body.client_ref;
  assert.strictEqual(r2a, r2b);
  assert.notStrictEqual(r2a, uploadRef, "new run → new ref");

  // no generator exposed → typed failure, no second UUID impl inside
  let capture3 = {};
  const s3 = run(capture3);
  delete s3.newOperationRef;
  let threw = null;
  try {
    await s3.runToolFlow(
      { id: "compress", label: "C", params: [], multi: false, pass_through: false },
      [file], {});
  } catch (e) { threw = e; }
  assert.ok(threw, "without app.js generator runToolFlow refuses to run");
  assert.ok(!capture3.upload, "no upload without the shared generator");

  // pass-through (share) flow also carries the upload client_ref
  let capture4 = {};
  const s4 = run(capture4);
  await s4.runToolFlow({ id: "share", label: "S", pass_through: true }, [file], {});
  assert.ok(capture4.upload, "pass-through upload happened");
  assert.ok(capture4.upload.form.get("client_ref"), "pass-through upload carries client_ref");

  console.log("PASS: process-flow upload+execute stable client_ref (5 groups)");
})().catch((e) => { console.error(e); process.exit(1); });
