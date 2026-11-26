// Optional real-browser end-to-end walkthrough (Playwright + Chromium).
//
//   1) pip install -r requirements.txt
//   2) ABX_DATA_DIR=/tmp/abx-dev python wsgi.py   # listens on :5000
//   3) cd tests && npm init -y && npm i playwright@1.49.0 && npx playwright install chromium
//   4) BASE=http://127.0.0.1:5000 node browser_walk.mjs
//
// It drives the real pages: sample load, canvas drag selection, six-segment
// cap, calibration, question opening, same-position A/B/X switching with
// actual WAV decoding, answer reveal, reselect/new question and old-question
// persistence, and writes screenshots to /tmp/pwtest/*.png.
import { chromium } from "playwright";

const BASE = process.env.BASE || "http://127.0.0.1:5000";
let failures = 0;
function ok(c, m) { console.log(c ? "  PASS:" : "  FAIL:", m); if (!c) failures++; }

const browser = await chromium.launch({
  headless: true,
  args: ["--no-sandbox", "--disable-dev-shm-usage", "--autoplay-policy=no-user-gesture-required"],
});
const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
const pageErrors = [];
page.on("pageerror", (e) => pageErrors.push(e.message));
page.on("console", (m) => { if (m.type() === "error") pageErrors.push(m.text()); });

// ---------- 1. home -> load samples ----------
console.log("[1] home + sample load");
await page.goto(BASE + "/", { waitUntil: "networkidle" });
ok(await page.locator('input[type=file]').count() === 2, "two file inputs visible");
ok(await page.locator('audio[src*="sample_A.wav"]').count() === 1, "sample A player present");
await page.screenshot({ path: "/tmp/pwtest/01-home.png", fullPage: true });

await Promise.all([
  page.waitForURL(/\/workspaces\/w_/, { timeout: 15000 }),
  page.click("#sample-form button"),
]);
const wid = page.url().split("/").find((p) => /^w_/.test(p));
ok(/^w_/.test(wid), "navigated to workspace " + wid);

// ---------- 2. segments page ----------
console.log("[2] segments page + real canvas drag");
await page.waitForSelector(".seg-row", { timeout: 10000 });
await page.waitForFunction(() => /已选 2 \/ 6/.test(document.querySelector("#seg-count").textContent));
ok(true, "two suggestion segments prefilled");
await page.waitForSelector("#open-question-btn:not([disabled])", { timeout: 10000 });
const gainText = await page.locator(".gain-value").first().textContent();
ok(/\d+\.\d+/.test(gainText), "feasible gain shown: " + gainText.trim());
await page.screenshot({ path: "/tmp/pwtest/02-segments-calibrated.png", fullPage: true });

// Real pointer drag on canvas A from 8%->20% width (inside phrase 1).
const box = await page.locator("#canvas-a").boundingBox();
await page.mouse.move(box.x + box.width * 0.08, box.y + box.height / 2);
await page.mouse.down();
for (let p = 0.10; p <= 0.201; p += 0.01) {
  await page.mouse.move(box.x + box.width * p, box.y + box.height / 2, { steps: 2 });
  await page.waitForTimeout(20);
}
await page.mouse.up();
await page.waitForFunction(() => /已选 3 \/ 6/.test(document.querySelector("#seg-count").textContent),
  null, { timeout: 5000 });
ok(await page.locator(".seg-row").count() === 3, "canvas drag added a third segment");
await page.waitForSelector("#calib-alert.alert.ok:not([hidden])", { timeout: 10000 });
ok(true, "recalibrated feasible after drag");
await page.screenshot({ path: "/tmp/pwtest/03-after-drag.png", fullPage: true });

// Cap at 6: add three more via button -> 6, add disabled.
for (let i = 0; i < 3; i++) {
  await page.click("#add-segment");
  await page.waitForTimeout(250);
}
ok(await page.locator(".seg-row").count() === 6, "can add up to six segments");
ok(await page.locator("#add-segment").isDisabled(), "add disabled at six");
// Remove the three button-added middle defaults (last 3) to restore drag+prefills.
for (let i = 0; i < 3; i++) {
  await page.locator('.seg-row [data-act="del"]').last().click();
  await page.waitForTimeout(250);
}
ok(await page.locator(".seg-row").count() === 3, "removed back to three");
await page.waitForSelector("#open-question-btn:not([disabled])", { timeout: 10000 });

// ---------- 3. open question ----------
console.log("[3] open ABX question");
await Promise.all([
  page.waitForURL(/\/questions\/q_/, { timeout: 10000 }),
  page.click("#open-question-btn"),
]);
const qid = page.url().split("/").pop();
await page.waitForSelector("#abx-panel:not([hidden])", { timeout: 10000 });
ok(true, "question page opened: " + qid);
const answerInHtml = await page.content();
ok(!/"x_is"\s*:/.test(answerInHtml), "no x_is in page source before answering");

// ---------- 4. same-position switching with real audio ----------
console.log("[4] A/B/X same-position switching");
const audio = page.locator("#player");
await page.waitForFunction(() => {
  const a = document.getElementById("player");
  return a.readyState >= 1 && isFinite(a.duration) && a.duration > 0;
}, null, { timeout: 10000 });
const dur = await audio.evaluate((a) => a.duration);
ok(Math.abs(dur - 15) < 0.05, "real WAV decoded, duration ~15s (" + dur.toFixed(2) + ")");

// Deterministic same-position check while paused (no wall-clock drift).
await audio.evaluate((a) => { a.pause(); a.currentTime = 5.0; });
await page.waitForFunction(() => Math.abs(document.getElementById("player").currentTime - 5.0) < 0.2,
  null, { timeout: 5000 });

await page.click('.src-btn[data-kind="B"]');
await page.waitForFunction(() => /\/audio\/B$/.test(document.getElementById("player").src));
await page.waitForFunction(() => {
  const a = document.getElementById("player");
  return a.readyState >= 1 && Math.abs(a.currentTime - 5.0) < 0.35;
}, null, { timeout: 5000 });
let tB = await audio.evaluate((a) => a.currentTime);
ok(Math.abs(tB - 5.0) < 0.35, "B seeks to same position 5.0s (got " + tB.toFixed(2) + ")");
let pausedB = await audio.evaluate((a) => a.paused);
ok(pausedB, "stayed paused when switching while paused");

// Playing state + progressing position are preserved across B -> A.
await audio.evaluate((a) => a.play());
await page.waitForTimeout(400);
await page.click('.src-btn[data-kind="A"]');
await page.waitForFunction(() => /\/audio\/A$/.test(document.getElementById("player").src));
await page.waitForTimeout(700);
let tA = await audio.evaluate((a) => a.currentTime);
let pausedA = await audio.evaluate((a) => a.paused);
ok(!pausedA, "A resumed playback after switch");
ok(tA >= 5.1 && tA < 7.0, "A continues from the same spot and advances (" + tA.toFixed(2) + ")");

await page.click('.src-btn[data-kind="X"]');
await page.waitForFunction(() => /\/audio\/X$/.test(document.getElementById("player").src));
await page.waitForTimeout(400);
ok(await page.locator('.src-btn[data-kind="X"].active').count() === 1, "X active");
await page.screenshot({ path: "/tmp/pwtest/04-abx.png", fullPage: true });

// Segment loop chip exists and toggles class.
const chipCount = await page.locator(".seg-chip").count();
ok(chipCount === 3, "three segment chips on question page");
await page.click(".seg-chip >> nth=0");
await page.waitForTimeout(300);
ok(await page.locator(".seg-chip.loop-on").count() === 1, "loop chip toggles on");

// ---------- 5. answer + reveal ----------
console.log("[5] submit judgment");
await page.check('input[name=choice][value="A"]');
await page.click("#submit-answer-btn");
await page.waitForSelector("#result:not([hidden])", { timeout: 8000 });
const result = await page.locator("#result").innerText();
ok(/正确答案：X 就是 [AB]/.test(result), "answer revealed: " + result.split("\n").slice(1, 3).join(" / "));
ok(/判断正确|判断错误/.test(result), "correctness shown");
const xSub = await page.locator('.src-btn[data-kind="X"] .sub').innerText();
ok(/答案：X = [AB]/.test(xSub), "X button reveals identity (" + xSub + ")");
await page.screenshot({ path: "/tmp/pwtest/05-result.png", fullPage: true });

// ---------- 6. reselect -> new question; old one persists ----------
console.log("[6] reselect and old-question persistence");
await page.click("#reselect-link");
await page.waitForURL(/\/workspaces\/w_/, { timeout: 8000 });
await page.waitForSelector(".seg-row");
// Remove all but one segment for a distinct second question.
while (await page.locator('.seg-row [data-act="del"]').count() > 1) {
  await page.locator('.seg-row [data-act="del"]').first().click();
  await page.waitForTimeout(250);
}
await page.waitForSelector("#open-question-btn:not([disabled])", { timeout: 10000 });
await Promise.all([
  page.waitForURL(/\/questions\/q_/, { timeout: 10000 }),
  page.click("#open-question-btn"),
]);
const qid2 = page.url().split("/").pop();
ok(qid2 !== qid, "opened a different question " + qid2);

// History page lists both.
await page.goto(BASE + "/questions", { waitUntil: "networkidle" });
ok(await page.locator(".qrow").count() >= 2, "history lists questions");
await page.screenshot({ path: "/tmp/pwtest/06-history.png", fullPage: true });

// Open the OLD question from history: it must still show its frozen result.
await page.goto(BASE + "/questions/" + qid, { waitUntil: "networkidle" });
await page.waitForSelector("#result:not([hidden])", { timeout: 8000 });
const oldResult = await page.locator("#result").innerText();
ok(/正确答案/.test(oldResult), "old question still shows its scored result");
ok(oldResult.includes("此前已提交"), "old question reports first-submission lock");
const oldSrc = await page.locator("#player").evaluate((a) => a.src);
ok(/\/audio\/X$/.test(oldSrc), "old question audio still served from snapshot");

ok(pageErrors.filter((e) => !e.includes("favicon")).length === 0,
  "no uncaught browser errors" + (pageErrors.length ? ": " + [...new Set(pageErrors)].join(" | ") : ""));

await browser.close();
console.log(`\n${failures === 0 ? "ALL PLAYWRIGHT CHECKS PASSED" : failures + " FAILURE(S)"}`);
process.exit(failures ? 1 : 0);
