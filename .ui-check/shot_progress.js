const { chromium } = require('playwright');
(async () => {
  const browser = await chromium.launch();
  const page = await (await browser.newContext({ viewport: { width: 1400, height: 900 } })).newPage();
  const errors = [];
  page.on('console', m => { if (m.type() === 'error') errors.push(m.text()); });
  page.on('pageerror', e => errors.push('[pageerror] ' + e.message));

  await page.goto('http://127.0.0.1:5500/login.html', { waitUntil: 'networkidle' });
  await page.fill('#email', 'test@test.com');
  await page.fill('#password', 'A@2024Lu');
  await page.click('#submit');
  await page.waitForURL(/dashboard\.html/, { timeout: 15000 });

  // Find a completed timetable id from the queue via API in-page
  const versionInfo = await page.evaluate(async () => {
    const res = await apiCall('GET', '/allocation/queue');
    const complete = res.jobs.find(j => j.status === 'complete' && j.timetable_id);
    return complete ? complete.timetable_id : null;
  });
  console.log('using timetable_id:', versionInfo);

  if (versionInfo) {
    await page.goto(`http://127.0.0.1:5500/progress.html?timetable=${versionInfo}`, { waitUntil: 'networkidle' });
    await page.waitForTimeout(2000);
    await page.screenshot({ path: 'shots/progress-complete.png', fullPage: true });
  }
  console.log('console errors:', JSON.stringify(errors));
  await browser.close();
})();
