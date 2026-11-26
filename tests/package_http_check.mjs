// End-to-end check for the ZIP packaging feature over real HTTP.
//
// It drives the same JSON API the segment page uses, downloads the exact
// stored ZIP, restores it with the same browser library
// (app/static/js/zip-restore.js), cross-checks SSE against the original
// sample WAVs, exercises reallocation and the 409 retention path.
//
//   ABX_DATA_DIR=/tmp/abx-dev PYTHONPATH=/tmp/pylibs python wsgi.py
//   BASE=http://127.0.0.1:5000 node tests/package_http_check.mjs
//
// Node >= 18 (global fetch / DecompressionStream) required; developed on 22.
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const ZipRestore = require("../app/static/js/zip-restore.js");

const BASE = process.env.BASE || "http://127.0.0.1:5000";
let failures = 0;
function ok(c, m) {
  console.log(c ? "  PASS:" : "  FAIL:", m);
  if (!c) failures++;
}

async function call(method, path, body) {
  const opts = { method, headers: { Accept: "application/json" } };
  if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const r = await fetch(BASE + path, opts);
  const j = await r.json().catch(() => ({}));
  return { status: r.status, body: j };
}

function sseAgainst(restored, original) {
  let sse = 0;
  const n = Math.min(restored.length, original.samples.length);
  for (let i = 0; i < n; i++) {
    const d = restored[i] - original.samples[i];
    sse += d * d;
  }
  return sse;
}

console.log("[1] create sample workspace");
let r = await call("POST", "/samples/use");
ok(r.status === 200 || r.status === 201, "sample workspace " + r.status);
const wid = r.body.workspace_id;
ok(/^w_/.test(wid || ""), "workspace id " + wid);

const info = await call("GET", `/api/workspaces/${wid}`);
ok(info.status === 200 && info.body.samples === 240000, "15 s / 240000 samples");

console.log("[2] generate package budget=300000 with circled 5-7s");
r = await call("POST", `/api/workspaces/${wid}/package`, {
  budget_bytes: 300000,
  segments: [{ start_sample: 80000, end_sample: 112000 }], // 5.0s-7.0s -> chunks 5,6
});
ok(r.status === 201, "created 201 (got " + r.status + " " + JSON.stringify(r.body.error || "") + ")");
let pkg = r.body.package;
ok(pkg && pkg.zip_bytes <= 300000, `zip ${pkg && pkg.zip_bytes} <= 300000`);
ok(pkg.intersection_chunks.join(",") === "5,6", "intersection chunks 5,6");
ok(pkg.chunks[5].bit_depth === 16 && pkg.chunks[6].bit_depth === 16, "chunks 5/6 are PCM16");
ok(pkg.chunks.length === 15, "15 chunks incl. exact 1s blocks");

console.log("[3] download exact ZIP and restore in JS");
const dl = await fetch(BASE + pkg.download_url);
ok(dl.status === 200, "download 200");
const zipBuf = await dl.arrayBuffer();
ok(zipBuf.byteLength === pkg.zip_bytes, "downloaded bytes match record");
const restored = await ZipRestore.restorePackage(zipBuf);
ok(restored.a.length === 240000 && restored.b.length === 240000, "restored A/B length 240000");
ok(restored.chunks.every((c, i) => c.index === i), "chunks ordered 0..14");

// Fetch originals and cross-check SSE + exactness of PCM16 chunks.
const origABuf = await (await fetch(BASE + "/sample-audio/sample_A.wav")).arrayBuffer();
const origBBuf = await (await fetch(BASE + "/sample-audio/sample_B.wav")).arrayBuffer();
const origA = ZipRestore.parseWav(new Uint8Array(origABuf));
const origB = ZipRestore.parseWav(new Uint8Array(origBBuf));
const nodeSse = sseAgainst(restored.a, origA) + sseAgainst(restored.b, origB);
ok(nodeSse === pkg.total_sse, `JS-computed SSE ${nodeSse} equals server ${pkg.total_sse}`);

// Every server-marked PCM16 chunk must restore bit-exact.
let exact16 = true;
for (const row of pkg.chunks) {
  if (row.bit_depth !== 16) continue;
  const from = row.start_sample, to = row.end_sample;
  for (let i = from; i < to; i++) {
    if (restored.a[i] !== origA.samples[i] || restored.b[i] !== origB.samples[i]) {
      exact16 = false; break;
    }
  }
}
ok(exact16, "all PCM16 chunks bit-exact vs originals");

console.log("[4] mark chunk 2 fidelity and reallocate at original cap");
r = await call("POST", `/api/workspaces/${wid}/package/reallocate`, { manual_chunks: [2] });
ok(r.status === 200, "reallocate 200 (got " + r.status + ")");
const pkg2 = r.body.package;
ok(pkg2.version === pkg.version + 1, "version bumped " + pkg.version + " -> " + pkg2.version);
ok(pkg2.chunks[2].bit_depth === 16 && pkg2.manual_chunks.includes(2), "chunk 2 now manual PCM16");
ok(pkg2.chunks[5].bit_depth === 16 && pkg2.chunks[6].bit_depth === 16, "intersection marks persist");
ok(pkg2.budget_bytes === 300000, "original cap retained");
ok(pkg2.zip_bytes <= 300000, "new zip within cap");
const dl2 = await (await fetch(BASE + pkg2.download_url + "?v=" + pkg2.version)).arrayBuffer();
const restored2 = await ZipRestore.restorePackage(dl2);
ok(restored2.chunks[2].bits === 16, "new ZIP chunk 2 is 16-bit");
const sse2 = sseAgainst(restored2.a, origA) + sseAgainst(restored2.b, origB);
ok(sse2 === pkg2.total_sse, "new package SSE cross-check");
// Forcing chunk 2 is a new constraint: it is bit-exact even if that displaces
// some other upgrade and total SSE rises versus the old unconstrained optimum.
let chunk2Exact = true;
for (let i = pkg2.chunks[2].start_sample; i < pkg2.chunks[2].end_sample; i++) {
  if (restored2.a[i] !== origA.samples[i] || restored2.b[i] !== origB.samples[i]) {
    chunk2Exact = false; break;
  }
}
ok(chunk2Exact, "manually forced chunk 2 restores bit-exact");

console.log("[5] impossible reallocation keeps previous package + marks");
const beforeBytes = Buffer.from(await (await fetch(BASE + pkg2.download_url)).arrayBuffer());
r = await call("POST", `/api/workspaces/${wid}/package/reallocate`, {
  manual_chunks: [0, 1, 2, 3, 4, 7, 8, 9, 10, 11, 12, 13, 14],
  budget_bytes: 1,
});
ok(r.status === 409 && r.body.code === "package_does_not_fit", "409 package_does_not_fit");
ok(r.body.retained === true && r.body.package.version === pkg2.version, "response carries retained v" + pkg2.version);
const cur = await call("GET", `/api/workspaces/${wid}/package`);
ok(cur.body.package.version === pkg2.version, "stored version unchanged after failure");
ok(JSON.stringify(cur.body.package.manual_chunks) === JSON.stringify([2]), "marks retained as [2]");
const afterBytes = Buffer.from(await (await fetch(BASE + pkg2.download_url)).arrayBuffer());
ok(beforeBytes.equals(afterBytes), "stored ZIP bytes unchanged after failure");

console.log("[6] invalid input handling");
r = await call("POST", `/api/workspaces/${wid}/package`, { budget_bytes: "lots" });
ok(r.status === 400 && r.body.code === "invalid_package", "bad budget -> 400");
r = await call("POST", `/api/workspaces/${wid}/package/reallocate`, { manual_chunks: [99] });
ok(r.status === 400, "chunk index out of range -> 400");

console.log(`\n${failures === 0 ? "ALL PACKAGE HTTP CHECKS PASSED" : failures + " FAILURE(S)"}`);
process.exit(failures ? 1 : 0);
