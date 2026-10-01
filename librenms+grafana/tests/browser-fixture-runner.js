// Optional Windows driver for the same HTML fixture tested by Chrome --dump-dom
// on Linux CI. Uses an existing workspace Playwright runtime, no app dependency.
const { chromium } = require(process.env.PLAYWRIGHT_MODULE_PATH || 'playwright');
(async () => {
  const [executablePath, url, width, height] = process.argv.slice(2);
  const browser = await chromium.launch({ executablePath, headless: true });
  try {
    const page = await browser.newPage({ viewport: { width: Number(width), height: Number(height) } });
    await page.goto(url);
    await page.waitForFunction(() => document.getElementById('browserResult')?.textContent, { timeout: 20000 });
    process.stdout.write(await page.content());
  } finally { await browser.close(); }
})().catch((error) => { console.error(error.message); process.exitCode = 1; });
