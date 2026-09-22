const { chromium } = require('playwright');
(async () => {
  const browser = await chromium.launch();
  const page = await (await browser.newContext({ viewport: { width: 390, height: 844 } })).newPage();
  await page.goto('http://127.0.0.1:5500/login.html', { waitUntil: 'networkidle' });
  await page.fill('#email', 'test@test.com');
  await page.fill('#password', 'A@2024Lu');
  await page.click('#submit');
  await page.waitForURL(/dashboard\.html/, { timeout: 15000 });
  await page.waitForTimeout(1000);
  await page.screenshot({ path: 'shots/mobile-dashboard-fixed.png', fullPage: true });
  await browser.close();
})();
