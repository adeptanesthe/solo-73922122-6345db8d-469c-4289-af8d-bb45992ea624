// Out-of-box first-click check for the packaging panel (Playwright).
// Unlike package_browser_walk.mjs this does NOT edit the prefilled segments or
// the default budget: it proves `生成打包` succeeds on a fresh sample workspace,
// restores audio from the ZIP blob, and re-hydrates the panel after reload.
//
//   BASE=http://127.0.0.1:5000 node tests/package_default_flow.mjs
import { chromium } from "playwright";

const BASE = process.env.BASE || "http://127.0.0.1:5000";
const fails = [];
const ok = (c, m) => {
  console.log(c ? "  PASS:" : "  FAIL:", m);
  if (!c) fails.push(m);
};

const browser = await chromium.launch({
  headless: true,
  args: ["--no-sandbox", "--disable-dev-shm-usage", "--autoplay-policy=no-user-gesture-required"],
});
const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
const errors = [];
page.on("pageerror", (e) => errors.push(e.message));

await page.goto(BASE + "/", { waitUntil: "networkidle" });
await Promise.all([
  page.waitForURL(/\/workspaces\/w_/, { timeout: 15000 }),
  page.click("#sample-form button"),
]);
await page.waitForSelector(".seg-row", { timeout: 10000 });
await page.waitForFunction(() =>
  /已选 2 \/ 6/.test(document.querySelector("#seg-count").textContent));

const defaultBudget = await page.inputValue("#pkg-budget");
ok(defaultBudget === "600000", "default budget 600000 (got " + defaultBudget + ")");

await page.click("#pkg-generate");
await page.waitForSelector("#pkg-panel:not([hidden])", { timeout: 30000 });
await page.waitForSelector(".pkg-chunk", { timeout: 10000 });
const alertCls = await page.locator("#pkg-alert").getAttribute("class");
ok(/alert ok/.test(alertCls || ""), "first-click generation succeeds with default prefilled segments");

const zipText = await page.locator("#pkg-bar-text").innerText();
const m = zipText.match(/ZIP ([\d,]+) B.*上限 ([\d,]+) B/);
ok(m && Number(m[1].replace(/,/g, "")) <= Number(m[2].replace(/,/g, "")),
  "bar shows ZIP within cap: " + zipText);

await page.waitForFunction(() => {
  const a = document.getElementById("pkg-audio-a");
  return a.src.startsWith("blob:") && a.readyState >= 1;
}, null, { timeout: 20000 });
ok(true, "default flow restores A audio from ZIP");

await page.reload({ waitUntil: "networkidle" });
await page.waitForSelector("#pkg-panel:not([hidden])", { timeout: 15000 });
ok(await page.locator(".pkg-chunk").count() === 15, "panel restored after reload from server record");

ok(errors.length === 0, "no page errors" + (errors.length ? ": " + errors.join(" | ") : ""));

await browser.close();
console.log(fails.length ? "DEFAULT FLOW FAIL" : "DEFAULT FLOW PASSED");
process.exit(fails.length ? 1 : 0);
