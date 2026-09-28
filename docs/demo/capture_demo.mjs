/**
 * capture_demo.mjs — auto-drive the console and record a walkthrough video.
 *
 * Produces docs/demo/demo.webm (convert to GIF below). This is the honest way
 * to get a demo GIF: drive the *real* running app, don't fake frames.
 *
 * Setup (once):
 *   npm i -D playwright && npx playwright install chromium
 * Run the app, then:
 *   node docs/demo/capture_demo.mjs           # expects console at :5174
 *
 * Convert to an optimized GIF (needs ffmpeg + gifsicle):
 *   ffmpeg -i docs/demo/demo.webm -vf "fps=12,scale=1000:-1:flags=lanczos" -f gif - \
 *     | gifsicle --optimize=3 --colors 200 > docs/demo/demo.gif
 */
import { chromium } from "playwright";

const URL = process.env.DEMO_URL || "http://localhost:5174";
const VIEW = { width: 1280, height: 800 };

// [nav button label, dwell ms, optional in-screen action]
const STORYBOARD = [
  ["Smart Order Routing", 1200, async (p) => {
    // toggle the ML fill model to show allocation shift
    await p.getByText(/ML fill model/).click().catch(() => {});
    await p.waitForTimeout(2600);
  }],
  ["Overview", 2000],
  ["Market Risk", 2400],
  ["Equity Derivatives", 1400, async (p) => {
    await p.getByText("American (LSM)").click().catch(() => {});
    await p.waitForTimeout(2600);
  }],
  ["Perps & Funding", 2400],
  ["Liquidation Risk", 2400],
  ["Observability", 3000],
  ["Risk Copilot", 1200, async (p) => {
    const q = p.getByText("Which position is closest to liquidation?");
    await q.click().catch(() => {});
    await p.waitForTimeout(3200);
  }],
];

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const run = async () => {
  const browser = await chromium.launch();
  const ctx = await browser.newContext({
    viewport: VIEW,
    recordVideo: { dir: "docs/demo/_raw", size: VIEW },
  });
  const page = await ctx.newPage();
  await page.goto(URL, { waitUntil: "networkidle" });
  await sleep(1200);

  for (const [label, dwell, action] of STORYBOARD) {
    const btn = page.getByRole("button", { name: label, exact: false }).first();
    await btn.click().catch(() => {});
    await sleep(600);
    if (action) await action(page);
    await sleep(dwell);
  }

  await ctx.close();
  await browser.close();
  console.log("recorded → docs/demo/_raw/*.webm  (rename to demo.webm, then convert to GIF)");
};

run();
