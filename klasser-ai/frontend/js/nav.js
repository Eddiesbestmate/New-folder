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
      ['data.html', 'School data'],
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

  topbar.replaceWith(shell);
  page.replaceWith(main);
  main.appendChild(page);
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
  }
})();
