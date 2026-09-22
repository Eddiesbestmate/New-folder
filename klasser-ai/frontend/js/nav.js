/* Klasser - shared sidebar navigation (BRAND.md "Sidebar").
 *
 * Replaces the per-page .topbar strip at load time and wraps the page body in
 * a flex shell. Doing it here rather than in 19 copies of the same markup
 * means a nav item is added once, and no page can drift out of sync.
 *
 * It must run before the page's own script, because it owns #who and #logout -
 * the ids every page binds to. Include it directly after js/auth.js.
 */

(function buildSidebar() {
  const topbar = document.querySelector('.topbar');
  const page = document.querySelector('.page');
  if (!topbar || !page) return;

  const GROUPS = [
    ['', [
      ['dashboard.html', 'Dashboard'],
      ['timetables.html', 'Timetables'],
      ['queue.html', 'Queue'],
    ]],
    ['School', [
      ['teachers.html', 'Teachers'],
      ['students.html', 'Students'],
      ['rooms.html', 'Rooms'],
      ['subjects.html', 'Subjects'],
      ['requirements.html', 'Class rules'],
      ['layout.html', 'Timetable layout'],
      ['transport.html', 'Transport'],
    ]],
    ['Admin', [
      ['onboarding.html', 'Setup'],
      ['users.html', 'Users'],
      ['billing.html', 'Billing'],
      ['notifications.html', 'Notifications'],
    ]],
  ];

  // Pages reachable from a link above but without their own nav entry. Keeping
  // them here means a child page still lights up its parent rather than
  // leaving the whole sidebar looking inactive.
  const PARENT_OF = {
    'progress.html': 'timetables.html',
    'output.html': 'timetables.html',
    'edit.html': 'timetables.html',
    'generate.html': 'timetables.html',
  };

  const here = window.location.pathname.split('/').pop() || 'dashboard.html';
  const active = PARENT_OF[here] || here;

  const link = (href, label) => {
    const a = document.createElement('a');
    a.className = 'sidebar-link' + (href === active ? ' active' : '');
    a.href = href;
    a.textContent = label;
    if (href === active) a.setAttribute('aria-current', 'page');
    return a;
  };

  const sidebar = document.createElement('nav');
  sidebar.className = 'sidebar';
  sidebar.setAttribute('aria-label', 'Main');

  const head = document.createElement('div');
  head.className = 'sidebar-head';
  const brand = document.createElement('a');
  brand.href = 'dashboard.html';
  brand.style.textDecoration = 'none';
  brand.innerHTML = '<span class="logo logo-light">Klass<span class="er">er</span></span>';
  head.appendChild(brand);

  const nav = document.createElement('div');
  nav.className = 'sidebar-nav';
  for (const [heading, items] of GROUPS) {
    if (heading) {
      const h = document.createElement('div');
      h.className = 'sidebar-group';
      h.textContent = heading;
      nav.appendChild(h);
    }
    items.forEach(([href, label]) => nav.appendChild(link(href, label)));
  }

  // Dev-only entries. Rendered now and revealed once the role is known, so the
  // sidebar does not reflow when /auth/me comes back.
  const devGroup = document.createElement('div');
  devGroup.className = 'sidebar-group';
  devGroup.textContent = 'Dev';
  devGroup.hidden = true;
  const devLinks = [link('dev.html', 'Dev portal'),
                    link('dev-support.html', 'Support queue')];
  devLinks.forEach((a) => { a.hidden = true; });
  nav.appendChild(devGroup);
  devLinks.forEach((a) => nav.appendChild(a));

  const foot = document.createElement('div');
  foot.className = 'sidebar-foot';
  foot.appendChild(link('support.html', 'Help'));

  // Same ids the old topbar exposed - every page binds to these.
  const who = document.createElement('span');
  who.className = 'who';
  who.id = 'who';

  const out = document.createElement('button');
  out.className = 'btn';
  out.id = 'logout';
  out.type = 'button';
  out.textContent = 'Log out';

  foot.appendChild(who);
  foot.appendChild(out);

  sidebar.appendChild(head);
  sidebar.appendChild(nav);
  sidebar.appendChild(foot);

  const shell = document.createElement('div');
  shell.className = 'shell';
  const main = document.createElement('div');
  main.className = 'sidebar-main';

  // A generation started from one page should stay visible from every other
  // one - leaving progress.html used to mean losing track of it entirely
  // until you thought to check queue.html again. It sits in the sidebar with
  // the rest of the app chrome rather than as a banner over the page, which
  // pushed the content down every time a run started.
  const statusBar = document.createElement('div');
  statusBar.className = 'gen-status-bar';
  statusBar.hidden = true;
  statusBar.setAttribute('aria-live', 'polite');
  statusBar.innerHTML =
    '<div class="gen-status-head">'
    + '<span class="gen-dot" aria-hidden="true"></span><span>Running</span>'
    + '</div>'
    + '<span id="gen-status-text"></span>'
    + '<a href="queue.html" id="gen-status-link">View progress</a>';

  topbar.replaceWith(shell);
  page.replaceWith(main);
  main.appendChild(page);
  sidebar.insertBefore(statusBar, foot);
  shell.appendChild(sidebar);
  shell.appendChild(main);

  if (typeof apiCall === 'function') {
    apiCall('GET', '/auth/me')
      .then((me) => {
        if (!me || me.role !== 'dev') return;
        devGroup.hidden = false;
        devLinks.forEach((a) => { a.hidden = false; });
      })
      .catch(() => { /* signed out or not provisioned - leave them hidden */ });

    pollGenerationStatus(statusBar);
    document.addEventListener('visibilitychange', () => {
      if (document.hidden) {
        clearInterval(genStatusTimer);
        genStatusTimer = null;
      } else if (!genStatusTimer) {
        pollGenerationStatus(statusBar);
      }
    });
  }
})();

let genStatusTimer = null;

async function pollGenerationStatus(bar) {
  const GEN_STATUS_POLL_MS = 6000;
  const LIVE = new Set(['queued', 'running']);
  const here = window.location.pathname.split('/').pop() || '';

  // The page itself already gives this a detailed, live view - a second,
  // slower-polling summary banner above it would be redundant noise.
  if (here === 'progress.html' || here === 'queue.html') return;

  const tick = async () => {
    try {
      const data = await apiCall('GET', '/allocation/queue');
      const mine = (data.jobs || []).filter((j) => LIVE.has(j.status));
      if (!mine.length) {
        bar.hidden = true;
        return;
      }
      const running = mine.filter((j) => j.status === 'running').length;
      const label = mine.length === 1
        ? `Generating "${mine[0].timetable_name || 'a timetable'}"...`
        : `${running} generation${running === 1 ? '' : 's'} running, `
          + `${mine.length - running} waiting...`;
      document.getElementById('gen-status-text').textContent = label;

      const link = document.getElementById('gen-status-link');
      link.href = mine.length === 1 && mine[0].timetable_id
        ? `progress.html?timetable=${encodeURIComponent(mine[0].timetable_id)}`
        : 'queue.html';

      bar.hidden = false;
    } catch {
      // A blip here should not put a false banner on every page in the app.
      bar.hidden = true;
    }
  };

  await tick();
  genStatusTimer = setInterval(tick, GEN_STATUS_POLL_MS);
}
