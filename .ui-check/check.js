const { chromium } = require('playwright');

const BASE = 'http://127.0.0.1:5500';
const EMAIL = 'test@test.com';
const PASSWORD = 'A@2024Lu';

const PAGES = [
  { name: 'dashboard', path: '/dashboard.html' },
  { name: 'queue', path: '/queue.html' },
  { name: 'timetables', path: '/timetables.html' },
  { name: 'requirements', path: '/requirements.html' },
  { name: 'transport', path: '/transport.html' },
  { name: 'data', path: '/data.html' },
  { name: 'layout', path: '/layout.html' },
  { name: 'onboarding', path: '/onboarding.html' },
  { name: 'billing', path: '/billing.html' },
  { name: 'users', path: '/users.html' },
  { name: 'notifications', path: '/notifications.html' },
  { name: 'import', path: '/import.html' },
  { name: 'export-templates', path: '/export-templates.html' },
  { name: 'support', path: '/support.html' },
];

(async () => {
  const browser = await chromium.launch();
  const context = await browser.newContext({ viewport: { width: 1400, height: 900 } });
  const page = await context.newPage();

  const report = {};
  const consoleErrors = {};
  let currentPage = 'boot';

  page.on('console', (msg) => {
    if (msg.type() === 'error') {
      (consoleErrors[currentPage] ||= []).push(msg.text());
    }
  });
  page.on('pageerror', (err) => {
    (consoleErrors[currentPage] ||= []).push('[pageerror] ' + err.message);
  });

  // --- Login ---
  currentPage = 'login';
  await page.goto(`${BASE}/login.html`, { waitUntil: 'networkidle' });
  await page.screenshot({ path: 'shots/00-login.png' });
  await page.fill('#email', EMAIL);
  await page.fill('#password', PASSWORD);
  await page.click('#submit');
  await page.waitForURL(/dashboard\.html/, { timeout: 15000 }).catch(() => {});
  await page.waitForTimeout(1500);
  await page.screenshot({ path: 'shots/01-post-login.png' });
  report.login = { url: page.url() };

  // --- Sidebar presence check on dashboard ---
  currentPage = 'dashboard';
  const sidebarExists = await page.locator('.sidebar').count();
  const sidebarLinks = await page.locator('.sidebar-link').allTextContents();
  report.sidebar = { exists: sidebarExists > 0, linkCount: sidebarLinks.length, links: sidebarLinks };

  // --- Walk every page ---
  for (const p of PAGES) {
    currentPage = p.name;
    const entry = { path: p.path };
    try {
      const resp = await page.goto(`${BASE}${p.path}`, { waitUntil: 'networkidle', timeout: 15000 });
      entry.httpStatus = resp ? resp.status() : null;
      await page.waitForTimeout(1200);

      // Layout sanity: does the page have visible horizontal overflow?
      entry.horizontalScroll = await page.evaluate(() => {
        return document.documentElement.scrollWidth > document.documentElement.clientWidth + 4;
      });

      // Is there a visible error/alert box shown unhidden?
      entry.visibleAlert = await page.evaluate(() => {
        const el = document.querySelector('#alert');
        if (!el) return null;
        const hidden = el.hasAttribute('hidden') || getComputedStyle(el).display === 'none';
        return hidden ? null : el.textContent.trim().slice(0, 300);
      });

      // Does the page still show "Loading..." after settling (stuck loading state)?
      entry.stuckLoading = await page.evaluate(() => {
        const el = document.querySelector('#loading');
        if (!el) return false;
        const hidden = el.hasAttribute('hidden') || getComputedStyle(el).display === 'none';
        return !hidden;
      });

      // Sidebar active-link check
      entry.sidebarActiveCount = await page.locator('.sidebar-link.active').count();

      // Broken images
      entry.brokenImages = await page.evaluate(() => {
        return Array.from(document.images)
          .filter((img) => !img.complete || img.naturalWidth === 0)
          .map((img) => img.src);
      });

      // Any element with literal "undefined" or "NaN" text (common template bugs)
      entry.suspiciousText = await page.evaluate(() => {
        const bodyText = document.body.innerText;
        const hits = [];
        if (/\bundefined\b/.test(bodyText)) hits.push('undefined');
        if (/\bNaN\b/.test(bodyText)) hits.push('NaN');
        if (/\[object Object\]/.test(bodyText)) hits.push('[object Object]');
        return hits;
      });

      await page.screenshot({ path: `shots/${p.name}.png`, fullPage: true });
    } catch (err) {
      entry.exception = err.message;
    }
    report[p.name] = entry;
  }

  report.consoleErrors = consoleErrors;

  // --- Mobile viewport spot-check on dashboard + queue ---
  await page.setViewportSize({ width: 390, height: 844 });
  currentPage = 'mobile-dashboard';
  await page.goto(`${BASE}/dashboard.html`, { waitUntil: 'networkidle' });
  await page.waitForTimeout(1000);
  await page.screenshot({ path: 'shots/mobile-dashboard.png', fullPage: true });
  report.mobileDashboardOverflow = await page.evaluate(() => {
    return document.documentElement.scrollWidth > document.documentElement.clientWidth + 4;
  });

  await browser.close();
  console.log(JSON.stringify(report, null, 2));
})();
