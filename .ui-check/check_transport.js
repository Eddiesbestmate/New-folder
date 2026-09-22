const { chromium } = require('playwright');
const BASE = 'http://127.0.0.1:5500';

(async () => {
  const browser = await chromium.launch();
  const page = await (await browser.newContext({ viewport: { width: 1400, height: 900 } })).newPage();
  const errors = [];
  page.on('console', m => { if (m.type() === 'error') errors.push(m.text()); });
  page.on('pageerror', e => errors.push('[pageerror] ' + e.message));

  await page.goto(`${BASE}/login.html`, { waitUntil: 'networkidle' });
  await page.fill('#email', 'test@test.com');
  await page.fill('#password', 'A@2024Lu');
  await page.click('#submit');
  await page.waitForURL(/dashboard\.html/, { timeout: 15000 });

  await page.goto(`${BASE}/transport.html`, { waitUntil: 'networkidle' });
  await page.waitForTimeout(800);

  // Go to Schedule tab, note the selected version
  await page.click('.tab[data-tab="schedule"]');
  await page.waitForTimeout(800);
  const versionBefore = await page.$eval('#version', el => el.value);
  const versionOptionsBefore = await page.$eval('#version', el => el.options.length);
  console.log('Schedule tab - version selected:', versionBefore, ' options:', versionOptionsBefore);

  // Manually change the version if there's more than one option, to make the test meaningful
  if (versionOptionsBefore > 1) {
    await page.selectOption('#version', { index: 1 });
    await page.waitForTimeout(500);
  }
  const chosenVersion = await page.$eval('#version', el => el.value);
  console.log('Chosen version for test:', chosenVersion);

  // Switch away to Fleet, then Routes, then back to Schedule
  await page.click('.tab[data-tab="fleet"]');
  await page.waitForTimeout(300);
  await page.click('.tab[data-tab="routes"]');
  await page.waitForTimeout(300);
  await page.click('.tab[data-tab="schedule"]');
  await page.waitForTimeout(800);

  const versionAfter = await page.$eval('#version', el => el.value);
  console.log('Version after switching away and back:', versionAfter);
  console.log('PRESERVED:', versionAfter === chosenVersion);

  console.log('console errors:', JSON.stringify(errors));
  await page.screenshot({ path: 'shots/transport-schedule.png' });
  await browser.close();
})();
