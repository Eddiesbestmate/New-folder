const { chromium } = require('playwright');
const BASE = 'http://127.0.0.1:5500';
const PAGES = ['dashboard','queue','timetables','requirements','transport','data',
               'layout','onboarding','billing','users','notifications','import',
               'export-templates','support','users','edit','output'];

(async () => {
  const browser = await chromium.launch();
  const page = await (await browser.newContext({ viewport: { width: 1400, height: 900 } })).newPage();
  const allErrors = {};
  let cur = 'boot';
  page.on('console', m => { if (m.type() === 'error') (allErrors[cur] ||= []).push(m.text()); });
  page.on('pageerror', e => (allErrors[cur] ||= []).push('[pageerror] ' + e.message));

  await page.goto(`${BASE}/login.html`, { waitUntil: 'networkidle' });
  await page.fill('#email', 'test@test.com');
  await page.fill('#password', 'A@2024Lu');
  await page.click('#submit');
  await page.waitForURL(/dashboard\.html/, { timeout: 15000 });

  const results = {};
  for (const name of PAGES) {
    cur = name;
    try {
      const resp = await page.goto(`${BASE}/${name}.html`, { waitUntil: 'networkidle', timeout: 12000 });
      await page.waitForTimeout(700);
      const overflow = await page.evaluate(() =>
        document.documentElement.scrollWidth - document.documentElement.clientWidth);
      results[name] = { status: resp ? resp.status() : null, overflowPx: overflow };
    } catch (e) {
      results[name] = { exception: e.message };
    }
  }
  console.log(JSON.stringify({ results, errors: allErrors }, null, 2));
  await browser.close();
})();
