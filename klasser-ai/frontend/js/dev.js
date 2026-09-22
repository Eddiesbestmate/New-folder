/* Klasser - dev portal.
 *
 * Every panel is read-only except four deliberate actions: refund a failed
 * generation, adjust a school's credits, decide an invoiced-billing
 * application, and change a setting or key. Each of those records who did it.
 */

const alertBox = document.getElementById('alert');
let schools = [];

document.getElementById('logout').addEventListener('click', logout);

function escapeHtml(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));
}

function when(value) {
  if (!value) return '';
  return new Date(value).toLocaleString(undefined,
    { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' });
}

function money(cents) {
  return `$${((cents || 0) / 100).toFixed(2)}`;
}

function chip(text, kind = '') {
  return `<span class="chip ${kind}">${escapeHtml(text)}</span>`;
}

function rows(id, html, columns, empty = 'Nothing here.') {
  // Number() on the colspan: it is always one of our own literals today, but
  // it lands in an attribute, and "always" is a property of the current
  // callers rather than of this function.
  document.getElementById(id).innerHTML = html
    || `<tr><td colspan="${Number(columns)}" class="small muted">`
       + `${escapeHtml(empty)}</td></tr>`;
}

/* --- Headline tiles -------------------------------------------------------- */

async function loadTiles() {
  const [health, gens, errs, owing] = await Promise.all([
    apiCall('GET', '/dev/health'),
    apiCall('GET', '/dev/generations', null, { limit: 1 }),
    apiCall('GET', '/dev/errors', null, { limit: 1 }),
    apiCall('GET', '/dev/outstanding', null, { limit: 200 }),
  ]);

  const pools = Object.entries(health.pools || {});
  const keyCount = pools.reduce((n, [, v]) => n + v, 0);

  document.getElementById('tiles').innerHTML = [
    [health.counts.schools, 'schools', ''],
    [health.counts.timetables, 'timetables', ''],
    [gens.totals.processing, 'running now', ''],
    [gens.totals.failed, 'failed', gens.totals.failed ? 'warn' : ''],
    [errs.totals.open, 'open errors', errs.totals.open ? 'bad' : ''],
    [errs.totals.our_fault, 'our fault', errs.totals.our_fault ? 'bad' : ''],
    [keyCount, 'live API keys', keyCount ? '' : 'bad'],
    [money(owing.total_owed_cents), 'owed', owing.total_owed_cents ? 'warn' : ''],
  ].map(([value, label, kind]) => `
    <div class="tile ${kind}"><b>${escapeHtml(String(value))}</b>
      <span>${label}</span></div>`).join('');

  if (!health.database || !health.encryption_configured) {
    showAlert(alertBox,
      `Database ${health.database ? 'ok' : 'DOWN'}, encryption `
      + `${health.encryption_configured ? 'ok' : 'NOT CONFIGURED'}.`);
  }
}

/* --- Generations ----------------------------------------------------------- */

async function loadGenerations() {
  const data = await apiCall('GET', '/dev/generations', null,
    { state: document.getElementById('gen-state').value, limit: 100 });

  rows('generations-rows', data.generations.map((g) => `
    <tr>
      <td>${escapeHtml(g.name)}
        ${g.last_failure
          ? `<div class="detail">${escapeHtml(g.last_failure.slice(0, 90))}</div>`
          : ''}</td>
      <td>${escapeHtml(g.school)}</td>
      <td>${chip(g.status, g.status === 'failed' ? 'bad'
        : g.status === 'complete' ? 'ok' : 'warn')}</td>
      <td>${g.attempts}</td>
      <td>${g.entries}</td>
      <td>${g.actual_credits ?? g.estimated_credits ?? '-'}
        ${g.billing ? `<div class="detail">${escapeHtml(g.billing)}</div>` : ''}</td>
      <td class="small muted">${when(g.created_at)}</td>
    </tr>`).join(''), 7);
}

/* --- Errors ---------------------------------------------------------------- */

async function loadErrors() {
  const data = await apiCall('GET', '/dev/errors', null, {
    resolved: document.getElementById('err-resolved').value,
    dev_fault: document.getElementById('err-fault').value,
    limit: 100,
  });

  rows('errors-rows', data.errors.map((e) => `
    <tr>
      <td>${escapeHtml(e.error_type)}
        ${e.provider ? ` (${escapeHtml(e.provider)})` : ''}
        <div class="detail">${escapeHtml((e.error_message || '').slice(0, 110))}</div>
      </td>
      <td>${escapeHtml(e.school || '-')}
        ${e.timetable ? `<div class="detail">${escapeHtml(e.timetable)}</div>` : ''}</td>
      <td>${chip(e.is_dev_fault ? 'ours' : 'school data',
        e.is_dev_fault ? 'bad' : '')}</td>
      <td>${e.billing_status === 'charged' ? e.actual_credits : '-'}</td>
      <td class="small muted">${when(e.created_at)}</td>
      <td style="display:flex;gap:6px">
        ${!e.resolved ? `<button class="btn small" data-resolve="${e.id}">Resolve</button>` : ''}
        ${!e.resolved && e.billing_status === 'charged'
          ? `<button class="btn small" data-refund="${e.id}">Refund</button>` : ''}
      </td>
    </tr>`).join(''), 6, 'No errors.');

  document.querySelectorAll('[data-resolve]').forEach((b) => {
    b.addEventListener('click', () => act(
      'POST', `/dev/errors/${b.dataset.resolve}/resolve`, 'Marked resolved.',
      loadErrors));
  });
  document.querySelectorAll('[data-refund]').forEach((b) => {
    b.addEventListener('click', () => {
      if (!confirm('Refund this school the credits that generation cost?')) return;
      act('POST', `/dev/errors/${b.dataset.refund}/refund`,
        'Refunded.', loadErrors);
    });
  });
}

async function act(method, path, message, after) {
  try {
    const result = await apiCall(method, path);
    showAlert(alertBox,
      result.already_refunded ? 'Already refunded.' : message, 'success');
    await after();
    await loadTiles();
  } catch (err) {
    showAlert(alertBox, err.message);
  }
}

/* --- Logs ------------------------------------------------------------------ */

async function loadLogs() {
  const data = await apiCall('GET', '/dev/logs', null, {
    stage: document.getElementById('log-stage').value, limit: 150,
  });

  rows('models-summary', data.last_7_days_by_model.map((m) => `
    <tr>
      <td>${escapeHtml(m.model_used)}</td>
      <td>${m.calls}</td>
      <td>${Number(m.tokens).toLocaleString()}</td>
      <td>${m.avg_ms ?? '-'} ms</td>
      <td>${m.failures ? chip(String(m.failures), 'bad') : '0'}</td>
    </tr>`).join(''), 5, 'No calls in the last 7 days.');

  rows('logs-rows', data.entries.map((l) => `
    <tr>
      <td class="small muted">${when(l.created_at)}</td>
      <td class="mono">${escapeHtml(l.stage)}</td>
      <td>${escapeHtml(l.model_used || '-')}</td>
      <td>${chip(l.status, l.status === 'ok' ? 'ok' : 'bad')}</td>
      <td>${l.tokens_used ?? '-'}</td>
      <td>${l.latency_ms ?? '-'}</td>
      <td class="small muted">${escapeHtml(l.school || '-')}</td>
    </tr>`).join(''), 7, 'Nothing logged yet.');
}

/* --- Schools --------------------------------------------------------------- */

async function loadSchools() {
  schools = await apiCall('GET', '/billing/dev/schools');

  rows('schools-rows', schools.map((s) => `
    <tr>
      <td>${escapeHtml(s.name)}</td>
      <td>${chip(s.billing_mode)}</td>
      <td>${s.balance}</td>
      <td>${s.reserved || 0}</td>
      <td>${s.lifetime_spent}</td>
      <td>${s.timetables}</td>
      <td>${s.account_blocked ? chip('blocked', 'bad') : chip('ok', 'ok')}</td>
      <td></td>
    </tr>`).join(''), 8);

  document.getElementById('adj-school').innerHTML = schools.map((s) =>
    `<option value="${s.id}">${escapeHtml(s.name)} (${s.balance} cr)</option>`
  ).join('');
}

document.getElementById('adjust').addEventListener('click', async () => {
  const note = document.getElementById('adj-note').value.trim();
  if (note.length < 3) {
    showAlert(alertBox, 'Say why - it is recorded against the account.');
    return;
  }
  try {
    await apiCall('POST', '/billing/dev/adjust', {
      school_id: document.getElementById('adj-school').value,
      amount: Number(document.getElementById('adj-amount').value),
      note,
    });
    showAlert(alertBox, 'Credits adjusted.', 'success');
    document.getElementById('adj-note').value = '';
    await loadSchools();
  } catch (err) {
    showAlert(alertBox, err.message);
  }
});

/* --- Users ----------------------------------------------------------------- */

async function loadUsers() {
  const data = await apiCall('GET', '/dev/users', null, {
    search: document.getElementById('user-search').value, limit: 200,
  });

  rows('users-rows', data.users.map((u) => `
    <tr>
      <td>${escapeHtml(u.first_name)} ${escapeHtml(u.surname)}</td>
      <td class="small">${escapeHtml(u.email)}</td>
      <td>${escapeHtml(u.school)}</td>
      <td>${chip(u.role, u.role === 'dev' ? 'warn' : '')}</td>
      <td>${u.is_active ? chip('active', 'ok') : chip('off', 'bad')}</td>
      <td class="small muted">${when(u.created_at)}</td>
    </tr>`).join(''), 6);
}

/* --- Money ----------------------------------------------------------------- */

async function loadMoney() {
  const [owing, invoices, applications, jobs] = await Promise.all([
    apiCall('GET', '/dev/outstanding', null, { limit: 200 }),
    apiCall('GET', '/dev/invoices', null, { limit: 100 }),
    apiCall('GET', '/dev/applications'),
    apiCall('GET', '/billing/dev/jobs', null, { limit: 10 }),
  ]);

  rows('outstanding-rows', owing.charges.map((c) => `
    <tr>
      <td>${escapeHtml(c.school)}
        ${c.account_blocked ? ' ' + chip('blocked', 'bad') : ''}</td>
      <td>${c.charge_type === 'payg_failed' ? 'Declined card' : 'Overdue invoice'}</td>
      <td>${c.days_outstanding}</td>
      <td>${money(c.original_amount_cents)}</td>
      <td>${money(c.interest_accrued_cents)}</td>
      <td><strong>${money(c.total_owed_cents)}</strong></td>
      <td><button class="btn small" data-settle="${c.id}">Mark paid</button></td>
    </tr>`).join(''), 7, 'Nobody owes anything.');

  document.querySelectorAll('[data-settle]').forEach((b) => {
    b.addEventListener('click', () => {
      if (!confirm('Record this debt as paid? The school is unblocked if it '
                   + 'was their last one.')) return;
      act('POST', `/billing/dev/outstanding/${b.dataset.settle}/resolve`,
        'Recorded as paid.', loadMoney);
    });
  });

  rows('invoices-rows', invoices.invoices.map((i) => `
    <tr>
      <td class="mono">${escapeHtml(i.invoice_number)}</td>
      <td>${escapeHtml(i.school)}</td>
      <td class="small">${i.period_start} to ${i.period_end}</td>
      <td class="small">${i.due_date}</td>
      <td>${chip(i.status + (i.days_overdue > 0 ? ` ${i.days_overdue}d` : ''),
        i.status === 'overdue' ? 'bad' : i.status === 'paid' ? 'ok' : '')}</td>
      <td>${money(i.total_cents)}</td>
    </tr>`).join(''), 6, 'No invoices raised.');

  document.getElementById('applications').innerHTML = applications.length
    ? applications.map((a) => `
      <div class="card" style="margin-bottom:8px">
        <div style="display:flex;justify-content:space-between;gap:10px">
          <div>
            <strong>${escapeHtml(a.school)}</strong> ${chip(a.status,
              a.status === 'approved' ? 'ok' : a.status === 'rejected' ? 'bad' : 'warn')}
            <div class="small muted">
              ${escapeHtml(a.billing_contact_name)} &middot;
              ${escapeHtml(a.billing_contact_email)}
              ${a.organisation_abn ? ` &middot; ABN ${escapeHtml(a.organisation_abn)}` : ''}
            </div>
            ${a.reason ? `<div class="small" style="margin-top:6px">${escapeHtml(a.reason)}</div>` : ''}
          </div>
          ${a.status === 'pending' ? `
            <div style="display:flex;gap:6px;flex:0 0 auto">
              <button class="btn small" data-approve="${a.id}">Approve</button>
              <button class="btn small" data-reject="${a.id}"
                      style="color:var(--text-danger)">Refuse</button>
            </div>` : ''}
        </div>
      </div>`).join('')
    : '<p class="small muted">No applications.</p>';

  document.querySelectorAll('[data-approve]').forEach((b) => {
    b.addEventListener('click', async () => {
      try {
        await apiCall('POST', `/dev/applications/${b.dataset.approve}/decide`,
          { approve: true });
        showAlert(alertBox, 'Approved. The school can now choose invoiced '
          + 'billing.', 'success');
        await loadMoney();
      } catch (err) { showAlert(alertBox, err.message); }
    });
  });
  document.querySelectorAll('[data-reject]').forEach((b) => {
    b.addEventListener('click', async () => {
      const reason = prompt('Why is this being refused? The school sees this.');
      if (!reason) return;
      try {
        await apiCall('POST', `/dev/applications/${b.dataset.reject}/decide`,
          { approve: false, rejection_reason: reason });
        showAlert(alertBox, 'Refused.', 'success');
        await loadMoney();
      } catch (err) { showAlert(alertBox, err.message); }
    });
  });

  document.getElementById('jobs').innerHTML = `
    <p class="small muted">
      ${jobs.scheduled.length
        ? jobs.scheduled.map((j) =>
          `${escapeHtml(j.id)} next ${when(j.next_run)}`).join(' &middot; ')
        : 'The scheduler runs in the worker; none is attached to this process.'}
    </p>
    <div class="wrap"><table class="table">
      <thead><tr><th>Job</th><th>Date</th><th>Status</th><th>By</th>
        <th>Result</th></tr></thead>
      <tbody>${jobs.history.map((h) => `
        <tr>
          <td class="mono">${escapeHtml(h.job_name)}</td>
          <td>${escapeHtml(h.run_date)}</td>
          <td>${chip(h.status, h.status === 'complete' ? 'ok' : 'bad')}</td>
          <td class="small muted">${escapeHtml(h.triggered_by)}</td>
          <td class="small muted">${escapeHtml(JSON.stringify(h.summary || {}).slice(0, 80))}</td>
        </tr>`).join('') || '<tr><td colspan="5" class="small muted">Not run yet.</td></tr>'}
      </tbody>
    </table></div>
    <div style="display:flex;gap:8px;margin-top:10px;flex-wrap:wrap">
      ${jobs.known.map((name) =>
        `<button class="btn small" data-job="${escapeHtml(name)}">Run ${escapeHtml(name)}</button>`
      ).join('')}
    </div>`;

  document.querySelectorAll('[data-job]').forEach((b) => {
    b.addEventListener('click', () => {
      if (!confirm(`Run ${b.dataset.job} now?`)) return;
      act('POST', `/billing/dev/jobs/${b.dataset.job}`, 'Job run.', loadMoney);
    });
  });
}

/* --- Models and keys ------------------------------------------------------- */

async function loadModels() {
  const [assignments, keys] = await Promise.all([
    apiCall('GET', '/dev/models'),
    apiCall('GET', '/dev/keys'),
  ]);

  const pools = assignments.pools || {};
  document.getElementById('pools').innerHTML = Object.keys(pools).length
    ? `<p class="small">${Object.entries(pools).map(([p, n]) =>
      `${escapeHtml(p)}: ${n} key${n === 1 ? '' : 's'}`).join(' &middot; ')}</p>`
    : '<p class="small muted">No keys loaded.</p>';

  rows('assign-rows', Object.entries(assignments.assignments || {}).map(
    ([task, alias]) => {
      const provider = (assignments.alias_providers || {})[alias];
      const count = pools[provider] || 0;
      return `
        <tr>
          <td class="mono">${escapeHtml(task)}</td>
          <td>
            <select data-task="${escapeHtml(task)}">
              ${(assignments.known_aliases || []).map((a) =>
                `<option value="${escapeHtml(a)}" ${a === alias ? 'selected' : ''}>`
                + `${escapeHtml(a)}</option>`).join('')}
            </select>
          </td>
          <td>${escapeHtml(provider || '?')}</td>
          <td>${count ? chip(`${count}`, 'ok') : chip('none', 'bad')}</td>
        </tr>`;
    }).join(''), 4);

  document.querySelectorAll('[data-task]').forEach((sel) => {
    sel.addEventListener('change', async () => {
      try {
        await apiCall('PUT', `/dev/settings/${sel.dataset.task}`,
          { value: sel.value });
        showAlert(alertBox, `${sel.dataset.task} now uses ${sel.value}.`,
          'success');
        await loadModels();
      } catch (err) {
        showAlert(alertBox, err.message);
        await loadModels();
      }
    });
  });

  rows('keys-rows', keys.map((k) => `
    <tr>
      <td>${escapeHtml(k.provider)}</td>
      <td>${escapeHtml(k.key_label)}</td>
      <td class="small muted">${escapeHtml(k.account_label || '')}</td>
      <td class="mono">${escapeHtml(k.hint || '')}</td>
      <td>${k.is_active ? chip('active', 'ok') : chip('off', 'bad')}</td>
      <td style="display:flex;gap:6px">
        <button class="btn small" data-toggle="${k.id}"
                data-to="${k.is_active ? 'false' : 'true'}">
          ${k.is_active ? 'Disable' : 'Enable'}</button>
        <button class="btn small" data-test="${escapeHtml(k.provider)}">Test</button>
        <button class="btn small" data-delkey="${k.id}"
                style="color:var(--text-danger)">Delete</button>
      </td>
    </tr>`).join(''), 6, 'No provider keys stored.');

  document.querySelectorAll('[data-toggle]').forEach((b) => {
    b.addEventListener('click', async () => {
      try {
        await apiCall('PUT', `/dev/keys/${b.dataset.toggle}/active`, null,
          { active: b.dataset.to });
        await loadModels();
        await loadTiles();
      } catch (err) { showAlert(alertBox, err.message); }
    });
  });
  document.querySelectorAll('[data-delkey]').forEach((b) => {
    b.addEventListener('click', async () => {
      if (!confirm('Delete this key? It cannot be recovered.')) return;
      try {
        await apiCall('DELETE', `/dev/keys/${b.dataset.delkey}`);
        showAlert(alertBox, 'Key deleted.', 'success');
        await loadModels();
        await loadTiles();
      } catch (err) { showAlert(alertBox, err.message); }
    });
  });
  document.querySelectorAll('[data-test]').forEach((b) => {
    b.addEventListener('click', async () => {
      const alias = (Object.entries(
        (await apiCall('GET', '/dev/models')).alias_providers || {})
        .find(([, p]) => p === b.dataset.test) || [])[0];
      if (!alias) { showAlert(alertBox, 'No model uses that provider.'); return; }
      b.disabled = true;
      b.textContent = 'Testing...';
      try {
        const result = await apiCall('POST', `/dev/providers/${alias}/test`);
        showAlert(alertBox, result.ok
          ? `${alias} answered in ${result.latency_ms}ms.`
          : `${alias} failed: ${result.error}`, result.ok ? 'success' : 'error');
      } catch (err) {
        showAlert(alertBox, err.message);
      } finally {
        b.disabled = false;
        b.textContent = 'Test';
      }
    });
  });
}

document.getElementById('add-key').addEventListener('click', async () => {
  const value = document.getElementById('key-value').value.trim();
  const label = document.getElementById('key-label').value.trim();
  if (!value || !label) {
    showAlert(alertBox, 'A label and the key itself are both needed.');
    return;
  }
  try {
    await apiCall('POST', '/dev/keys', {
      provider: document.getElementById('key-provider').value,
      key_label: label,
      api_key: value,
      account_label: document.getElementById('key-account').value.trim() || null,
    });
    showAlert(alertBox, 'Key added and loaded into the pool.', 'success');
    document.getElementById('key-value').value = '';
    document.getElementById('key-label').value = '';
    await loadModels();
    await loadTiles();
  } catch (err) {
    showAlert(alertBox, err.message);
  }
});

/* --- Settings -------------------------------------------------------------- */

let allSettings = [];

async function loadSettings() {
  allSettings = await apiCall('GET', '/dev/settings');
  renderSettings();
}

function renderSettings() {
  const filter = document.getElementById('setting-search').value.toLowerCase();
  const shown = allSettings.filter((s) =>
    !filter || s.key.toLowerCase().includes(filter)
    || (s.category || '').toLowerCase().includes(filter));

  rows('settings-rows', shown.map((s) => `
    <tr>
      <td class="mono">${escapeHtml(s.key)}</td>
      <td><input data-setting="${escapeHtml(s.key)}"
                 value="${escapeHtml(s.value)}" style="margin:0;width:120px"></td>
      <td>${chip(s.category || '')}</td>
      <td class="small muted">${escapeHtml(s.description || '')}</td>
    </tr>`).join(''), 4, 'Nothing matches.');

  document.querySelectorAll('[data-setting]').forEach((el) => {
    el.addEventListener('change', async () => {
      try {
        await apiCall('PUT', `/dev/settings/${el.dataset.setting}`,
          { value: el.value });
        showAlert(alertBox, `${el.dataset.setting} saved.`, 'success');
        const found = allSettings.find((s) => s.key === el.dataset.setting);
        if (found) found.value = el.value;
      } catch (err) {
        showAlert(alertBox, err.message);
        await loadSettings();
      }
    });
  });
}

document.getElementById('setting-search')
  .addEventListener('input', renderSettings);

/* --- Tabs ------------------------------------------------------------------ */

const LOADERS = {
  generations: loadGenerations, errors: loadErrors, logs: loadLogs,
  schools: loadSchools, users: loadUsers, money: loadMoney,
  models: loadModels, settings: loadSettings,
};

const loaded = new Set();

document.querySelectorAll('.tab').forEach((tab) => {
  tab.addEventListener('click', async () => {
    document.querySelectorAll('.tab').forEach((t) =>
      t.classList.toggle('active', t === tab));
    Object.keys(LOADERS).forEach((name) => {
      document.getElementById(`panel-${name}`).hidden = name !== tab.dataset.tab;
    });

    // Loaded on first open rather than all at once: the logs and generations
    // queries are the expensive ones and most visits want neither.
    const name = tab.dataset.tab;
    if (!loaded.has(name)) {
      loaded.add(name);
      try {
        await LOADERS[name]();
      } catch (err) {
        loaded.delete(name);
        showAlert(alertBox, err.message);
      }
    }
  });
});

document.querySelectorAll('[data-reload]').forEach((b) => {
  b.addEventListener('click', () => LOADERS[b.dataset.reload]()
    .catch((err) => showAlert(alertBox, err.message)));
});

['gen-state', 'err-resolved', 'err-fault'].forEach((id) => {
  document.getElementById(id).addEventListener('change', () => {
    const panel = id.startsWith('gen') ? loadGenerations : loadErrors;
    panel().catch((err) => showAlert(alertBox, err.message));
  });
});

/* --- Load ------------------------------------------------------------------ */

(async () => {
  const me = await requireLogin();
  if (!me) return;

  document.getElementById('who').textContent =
    `${me.first_name} ${me.surname} - ${me.role}`;

  if (me.role !== 'dev') {
    document.getElementById('loading').hidden = true;
    showAlert(alertBox, 'This is the dev portal. Your account is not a dev '
      + 'account.');
    return;
  }

  const models = await apiCall('GET', '/dev/models');
  document.getElementById('key-provider').innerHTML =
    [...new Set(Object.values(models.alias_providers || {}))]
      .map((p) => `<option value="${escapeHtml(p)}">${escapeHtml(p)}</option>`)
      .join('');

  await loadTiles();
  await loadGenerations();
  loaded.add('generations');

  document.getElementById('loading').hidden = true;
  document.getElementById('content').hidden = false;
})().catch((err) => {
  document.getElementById('loading').hidden = true;
  showAlert(alertBox, err.message);
});
