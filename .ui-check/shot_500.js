const { chromium } = require('playwright');
(async () => {
  const browser = await chromium.launch();
  const page = await (await browser.newContext({ viewport: { width: 500, height: 900 } })).newPage();
  await page.goto('http://127.0.0.1:5500/login.html', { waitUntil: 'networkidle' });
  await page.fill('#email', 'test@test.com');
  await page.fill('#password', 'A@2024Lu');
  await page.click('#submit');
  await page.waitForURL(/dashboard\.html/, { timeout: 15000 });
  await page.waitForTimeout(1000);
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
  console.log('overflow px:', overflow);
  await page.screenshot({ path: 'shots/500px-dashboard.png' });
  await browser.close();
})();
