// Real-browser walkthrough for the ZIP packaging panel (Playwright).
//
// Setup is the same as browser_walk.mjs:
//   pip install -r requirements.txt && python wsgi.py
//   cd tests && npm i playwright@1.49.0 && npx playwright install chromium
//   BASE=http://127.0.0.1:5000 node package_browser_walk.mjs
import { chromium } from "playwright";
import fs from "node:fs";

const BASE = process.env.BASE || "http://127.0.0.1:5000";
let failures = 0;
function ok(c, m) { console.log(c ? "  PASS:" : "  FAIL:", m); if (!c) failures++; }

const browser = await chromium.launch({
  headless: true,
  args: ["--no-sandbox", "--disable-dev-shm-usage", "--autoplay-policy=no-user-gesture-required"],
});
const page = await browser.newPage({ viewport: { width: 1360, height: 1000 } });
const pageErrors = [];
page.on("pageerror", (e) => pageErrors.push(e.message));
page.on("console", (m) => { if (m.type() === "error") pageErrors.push(m.text()); });

console.log("[1] load samples -> segments page");
await page.goto(BASE + "/", { waitUntil: "networkidle" });
await Promise.all([
  page.waitForURL(/\/workspaces\/w_/, { timeout: 15000 }),
  page.click("#sample-form button"),
]);
await page.waitForSelector(".seg-row", { timeout: 10000 });

// Force one deterministic fidelity segment 5.00s-7.00s and remove the other.
console.log("[2] set exact segment 5.00-7.00s");
while (await page.locator(".seg-row").count() > 1) {
  await page.locator('.seg-row [data-act="del"]').last().click();
  await page.waitForTimeout(200);
}
const row = page.locator(".seg-row").first();
await row.locator('[data-field="start"]').fill("5.00");
await row.locator('[data-field="end"]').fill("7.00");
await row.locator('[data-field="end"]').blur();
await page.waitForTimeout(600);
ok(/已选 1 \/ 6/.test(await page.locator("#seg-count").textContent()), "one segment selected");

console.log("[3] generate package budget=300000");
await page.fill("#pkg-budget", "300000");
await page.click("#pkg-generate");
await page.waitForSelector("#pkg-panel:not([hidden])", { timeout: 30000 });
await page.waitForSelector(".pkg-chunk", { timeout: 10000 });
ok(await page.locator(".pkg-chunk").count() === 15, "15 chunk rows rendered");

// In-browser ZIP restore -> real WAV blob decoded to ~15 s duration.
await page.waitForFunction(() => {
  const a = document.getElementById("pkg-audio-a");
  return a && a.src.startsWith("blob:") && a.readyState >= 1 &&
         isFinite(a.duration) && Math.abs(a.duration - 15) < 0.1;
}, null, { timeout: 20000 });
ok(true, "restored A from ZIP decodes to ~15.00s WAV in Chromium");
const durB = await page.locator("#pkg-audio-b").evaluate((v) =>
  new Promise((res) => {
    if (v.readyState >= 1) return res(v.duration);
    v.onloadedmetadata = () => res(v.duration);
    setTimeout(() => res(v.duration), 8000);
  }));
ok(Math.abs(durB - 15) < 0.1, "restored B ~15.00s (" + durB.toFixed(2) + ")");

const restoreNote = await page.locator("#pkg-restore-note").innerText();
ok(/已在网页内从同一份 ZIP 还原/.test(restoreNote), "success restore note shown");

// Intersection chunks 5 and 6 are checked + disabled.
const inter = await page.evaluate(() => {
  return [5, 6].map((i) => {
    const cb = document.querySelector('.pkg-check[data-index="' + i + '"]');
    return { checked: cb.checked, disabled: cb.disabled };
  });
});
ok(inter.every((x) => x.checked && x.disabled), "chunks 5/6 checked+disabled (intersection)");

const summary = await page.locator("#pkg-summary").innerText();
ok(/PCM16 × \d+/.test(summary) && /PCM8 × \d+/.test(summary), "depth summary rendered");
await page.screenshot({ path: "/tmp/pwtest/pkg-01-generated.png", fullPage: true });

console.log("[4] download the exact same blob ZIP");
const [download] = await Promise.all([
  page.waitForEvent("download", { timeout: 10000 }),
  page.click("#pkg-download"),
]);
const suggested = download.suggestedFilename();
ok(/^abx-package-.*-v1\.zip$/.test(suggested), "download filename " + suggested);
const dlPath = "/tmp/pwtest/" + suggested;
await download.saveAs(dlPath);
const head = fs.readFileSync(dlPath).subarray(0, 2).toString("latin1");
ok(head === "PK", "downloaded file is a ZIP (PK magic)");
const dlSize = fs.statSync(dlPath).size;
ok(dlSize <= 300000, "downloaded ZIP within cap (" + dlSize + " B)");

console.log("[5] mark chunk 2 fidelity -> reallocate");
await page.check('.pkg-check[data-index="2"]');
await page.click("#pkg-realloc");
await page.waitForSelector("#pkg-realloc-alert.alert.ok:not([hidden])", { timeout: 30000 });
const okMsg = await page.locator("#pkg-realloc-alert").innerText();
ok(/新包已生成/.test(okMsg), "success reallocation message");
await page.waitForFunction(() => {
  const el = document.querySelector('.pkg-chunk[data-index="2"] .pill.ok') ||
             document.querySelector('.pkg-chunk.is16 .pkg-check[data-index="2"]');
  const cb = document.querySelector('.pkg-check[data-index="2"]');
  return cb && cb.checked && !cb.disabled &&
         document.querySelector('.pkg-chunk[data-index="2"]').classList.contains("is16");
}, null, { timeout: 10000 });
ok(true, "chunk 2 shows PCM16 + checked after reallocation");
ok(/v2/.test(okMsg), "panel updated to v2");
await page.screenshot({ path: "/tmp/pwtest/pkg-02-reallocated.png", fullPage: true });

console.log("[6] force every optional chunk -> impossible at 300000");
const optional = page.locator(".pkg-check:not(:disabled)");
const nOpt = await optional.count();
for (let i = 0; i < nOpt; i++) await optional.nth(i).check();
await page.click("#pkg-realloc");
await page.waitForSelector("#pkg-realloc-alert.alert.error:not([hidden])", { timeout: 30000 });
const errMsg = await page.locator("#pkg-realloc-alert").innerText();
ok(/放不下/.test(errMsg) && /保留/.test(errMsg), "infeasible prompt says retained: " + errMsg.slice(0, 40) + "…");

// UI must have re-synced to retained v2 marks: only chunk 2 enabled+checked.
await page.waitForTimeout(500);
const retained = await page.evaluate(() => {
  const boxes = [...document.querySelectorAll(".pkg-check")];
  return {
    checkedEnabled: boxes.filter((b) => b.checked && !b.disabled).map((b) => +b.dataset.index),
    versionText: document.getElementById("pkg-realloc-alert").textContent,
  };
});
ok(JSON.stringify(retained.checkedEnabled) === "[2]",
  "marks rolled back to retained [2], got " + JSON.stringify(retained.checkedEnabled));
const chunk2Is16 = await page.locator('.pkg-chunk[data-index="2"].is16').count();
ok(chunk2Is16 === 1, "retained package still renders chunk2 PCM16");

console.log("[7] preset fills budget");
await page.click('#pkg-presets [data-bytes="450000"]');
ok((await page.inputValue("#pkg-budget")) === "450000", "450KB preset fills input");

const seriousErrors = pageErrors.filter((e) =>
  !e.includes("favicon") && !e.includes("Download is disallowed") &&
  // Expected + handled by the page: initial no-package probe (404) and the
  // deliberate impossible-reallocation response (409).
  !/status of 404/.test(e) && !/status of 409/.test(e) &&
  // blob download navigation noise is not a page error in practice.
  !e.includes("Autofocus processing was blocked"));
ok(seriousErrors.length === 0,
  "no uncaught browser errors" + (seriousErrors.length ? ": " + [...new Set(seriousErrors)].join(" | ") : ""));

await browser.close();
console.log(`\n${failures === 0 ? "ALL PACKAGE PLAYWRIGHT CHECKS PASSED" : failures + " FAILURE(S)"}`);
process.exit(failures ? 1 : 0);
