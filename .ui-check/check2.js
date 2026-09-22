const { chromium } = require('playwright');
const BASE = 'http://127.0.0.1:5500';
(async () => {
  const b = await chromium.launch();
  const page = await (await b.newContext({ viewport: { width: 1400, height: 900 } })).newPage();
  const errs = [];
  page.on('pageerror', e => errs.push(e.message));
  await page.goto(`${BASE}/login.html`, { waitUntil: 'networkidle' });
  await page.fill('#email', 'test@test.com');
  await page.fill('#password', 'A@2024Lu');
  await page.click('#submit');
  await page.waitForURL(/dashboard\.html/, { timeout: 15000 });
  await page.goto(`${BASE}/transport.html`, { waitUntil: 'networkidle' });
  await page.waitForTimeout(1000);

  // Force the status indicator visible so its sidebar styling can be checked
  // even though no generation is running right now.
  await page.evaluate(() => {
    const bar = document.querySelector('.gen-status-bar');
    document.getElementById('gen-status-text').textContent = 'Generating "Curriculum verification run"...';
    bar.hidden = false;
  });
  await page.waitForTimeout(200);

  const inSidebar = await page.evaluate(() =>
    !!document.querySelector('.sidebar > .gen-status-bar'));
  const cbWidth = await page.$eval('#allow-cross', el => el.getBoundingClientRect().width);
  console.log('status bar inside sidebar:', inSidebar);
  console.log('checkbox width px:', Math.round(cbWidth));
  console.log('pageerrors:', JSON.stringify(errs));
  await page.screenshot({ path: 'shots/transport-fixed.png' });
  await b.close();
})();
