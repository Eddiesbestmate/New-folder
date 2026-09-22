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

  const widths = await page.evaluate(() => {
    const out = {};
    out.docScrollWidth = document.documentElement.scrollWidth;
    out.docClientWidth = document.documentElement.clientWidth;
    out.bodyScrollWidth = document.body.scrollWidth;
    for (const sel of ['.shell', '.sidebar', '.sidebar-main', '.page', '.card', '.feature-grid']) {
      const el = document.querySelector(sel);
      if (!el) { out[sel] = 'MISSING'; continue; }
      const cs = getComputedStyle(el);
      out[sel] = {
        offsetWidth: el.offsetWidth,
        scrollWidth: el.scrollWidth,
        display: cs.display,
        maxWidth: cs.maxWidth,
        width: cs.width,
      };
    }
    return out;
  });
  console.log(JSON.stringify(widths, null, 2));
  await browser.close();
})();
