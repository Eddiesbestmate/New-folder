const { chromium } = require('playwright');
(async () => {
  const browser = await chromium.launch();
  const page = await (await browser.newContext()).newPage();
  await page.goto('http://127.0.0.1:5500/login.html', { waitUntil: 'networkidle' });
  await page.fill('#email', 'test@test.com');
  await page.fill('#password', 'A@2024Lu');
  await page.click('#submit');
  await page.waitForURL(/dashboard\.html/, { timeout: 15000 });
  await page.waitForTimeout(1500);

  const visible = await page.locator('.sidebar-link:visible').allTextContents();
  const devPortalVisible = await page.locator('a[href="dev.html"]').isVisible();
  const devPortalHiddenAttr = await page.locator('a[href="dev.html"]').getAttribute('hidden');

  console.log('visible sidebar links:', JSON.stringify(visible));
  console.log('dev portal link isVisible():', devPortalVisible);
  console.log('dev portal link hidden attr:', devPortalHiddenAttr);
  await browser.close();
})();
