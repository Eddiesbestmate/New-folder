const { chromium } = require('playwright');
const BASE = 'http://127.0.0.1:5500';
const PAGES = ['dashboard','queue','timetables','requirements','transport',
               'export-templates','notifications','output','progress'];

(async () => {
  const browser = await chromium.launch();
  const page = await (await browser.newContext({ viewport: { width: 390, height: 844 } })).newPage();
  await page.goto(`${BASE}/login.html`, { waitUntil: 'networkidle' });
  await page.fill('#email', 'test@test.com');
  await page.fill('#password', 'A@2024Lu');
  await page.click('#submit');
  await page.waitForURL(/dashboard\.html/, { timeout: 15000 });

  for (const name of PAGES) {
    await page.goto(`${BASE}/${name}.html`, { waitUntil: 'networkidle', timeout: 15000 }).catch(()=>{});
    await page.waitForTimeout(900);
    const overflow = await page.evaluate(() => {
      const docW = document.documentElement.scrollWidth - document.documentElement.clientWidth;
      if (docW <= 4) return null;
      // find the widest offending element
      let worst = null, worstDiff = 0;
      document.querySelectorAll('body *').forEach(el => {
        const diff = el.scrollWidth - el.clientWidth;
        if (diff > worstDiff && el.children.length < 15) {
          worstDiff = diff;
          worst = { tag: el.tagName, cls: el.className, diff, outer: el.outerHTML.slice(0,150) };
        }
      });
      return { docOverflowPx: docW, worst };
    });
    console.log(name.padEnd(18), overflow ? JSON.stringify(overflow) : 'OK, no overflow');
  }
  await browser.close();
})();
