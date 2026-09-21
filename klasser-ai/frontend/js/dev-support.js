/* Klasser AI - dev portal support queue. */

const alertBox = document.getElementById('alert');
let current = null;

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

function age(value) {
  const hours = (Date.now() - new Date(value)) / 36e5;
  if (hours < 1) return `${Math.round(hours * 60)}m`;
  if (hours < 48) return `${Math.round(hours)}h`;
  return `${Math.round(hours / 24)}d`;
}

/* --- Queue ----------------------------------------------------------------- */

async function loadQueue() {
  const params = {};
  const status = document.getElementById('f-status').value;
  const priority = document.getElementById('f-priority').value;
  const category = document.getElementById('f-category').value;
  if (status) params.status = status;
  if (priority) params.priority = priority;
  if (category) params.category = category;

  const data = await apiCall('GET', '/support/dev/tickets', null, params);

  const t = data.totals;
  document.getElementById('totals').innerHTML = `
    <span class="stat"><b>${t.open}</b> open</span>
    <span class="stat"><b>${t.in_progress}</b> in progress</span>
    <span class="stat"><b>${t.urgent}</b> urgent unresolved</span>
    <span class="stat"><b>${t.resolved}</b> resolved</span>`;

  const box = document.getElementById('rows');
  if (!data.tickets.length) {
    box.innerHTML = '<p class="small muted">Nothing matching those filters.</p>';
    return;
  }

  box.innerHTML = data.tickets.map((x) => `
    <div class="row ${current === x.id ? 'active' : ''}" data-id="${x.id}">
      <div style="display:flex;justify-content:space-between;gap:10px">
        <span class="subject">${escapeHtml(x.subject)}</span>
        <span class="chip ${escapeHtml(x.priority)}">${escapeHtml(x.priority)}</span>
      </div>
      <div class="small muted">
        ${escapeHtml(x.school || 'unknown school')} &middot;
        ${escapeHtml(x.category)} &middot; ${age(x.created_at)} old &middot;
        ${escapeHtml(x.status.replace('_', ' '))}
        ${x.has_notes ? '&middot; has notes' : ''}
      </div>
    </div>`).join('');

  document.querySelectorAll('[data-id]').forEach((row) => {
    row.addEventListener('click', () => open(row.dataset.id));
  });
}

/* --- One ticket ------------------------------------------------------------ */

async function open(id) {
  current = id;
  const data = await apiCall('GET', `/support/dev/tickets/${id}`);
  const t = data.ticket;

  document.getElementById('detail').hidden = false;
  document.getElementById('d-subject').textContent = t.subject;
  document.getElementById('d-meta').innerHTML =
    `${escapeHtml(t.school || 'unknown school')} &middot; `
    + `${escapeHtml(t.raised_by_name || '')} (${escapeHtml(t.raised_by || '')})`
    + ` &middot; ${escapeHtml(t.category)} &middot; raised ${when(t.created_at)}`
    + (t.timetable_name ? ` &middot; ${escapeHtml(t.timetable_name)}` : '')
    + (t.resolved_at
      ? `<br>Resolved ${when(t.resolved_at)} by ${escapeHtml(t.resolved_by || '')}`
      : '');
  document.getElementById('d-body').textContent = t.body;
  document.getElementById('d-status').value = t.status;
  document.getElementById('d-priority').value = t.priority;
  document.getElementById('d-notes').value = t.internal_notes || '';

  const logWrap = document.getElementById('d-log-wrap');
  logWrap.hidden = !data.pipeline_log.length;
  document.getElementById('d-log').innerHTML = data.pipeline_log.map((l) =>
    `${when(l.created_at)}  ${escapeHtml(l.stage)}  ${escapeHtml(l.status)}`
    + `${l.model_used ? `  ${escapeHtml(l.model_used)}` : ''}`
    + `${l.latency_ms ? `  ${l.latency_ms}ms` : ''}`
    + `${l.detail ? `<br>    ${escapeHtml(String(l.detail).slice(0, 200))}` : ''}`
  ).join('<br>');

  const errWrap = document.getElementById('d-err-wrap');
  errWrap.hidden = !data.errors.length;
  document.getElementById('d-errors').innerHTML = data.errors.map((e) =>
    `${when(e.created_at)}  ${escapeHtml(e.error_type)}`
    + `${e.provider ? ` (${escapeHtml(e.provider)})` : ''}`
    + `  ${e.is_dev_fault ? 'our fault' : 'school data'}`
    + `<br>    ${escapeHtml(String(e.error_message || '').slice(0, 300))}`
  ).join('<br>');

  await loadQueue();
}

document.getElementById('save').addEventListener('click', async () => {
  if (!current) return;
  const button = document.getElementById('save');
  button.disabled = true;
  try {
    await apiCall('PATCH', `/support/dev/tickets/${current}`, {
      status: document.getElementById('d-status').value,
      priority: document.getElementById('d-priority').value,
      internal_notes: document.getElementById('d-notes').value || null,
    });
    showAlert(alertBox, 'Ticket updated.', 'success');
    await open(current);
  } catch (err) {
    showAlert(alertBox, err.message);
  } finally {
    button.disabled = false;
  }
});

['f-status', 'f-priority', 'f-category'].forEach((id) => {
  document.getElementById(id).addEventListener('change',
    () => loadQueue().catch((e) => showAlert(alertBox, e.message)));
});
document.getElementById('refresh').addEventListener('click',
  () => loadQueue().catch((e) => showAlert(alertBox, e.message)));

/* --- Load ------------------------------------------------------------------ */

(async () => {
  const me = await requireLogin();
  if (!me) return;

  document.getElementById('who').textContent =
    `${me.first_name} ${me.surname} - ${me.role}`;

  if (me.role !== 'dev') {
    document.getElementById('loading').hidden = true;
    showAlert(alertBox, 'This page is for the dev portal.');
    return;
  }

  await loadQueue();

  document.getElementById('loading').hidden = true;
  document.getElementById('content').hidden = false;
})().catch((err) => {
  document.getElementById('loading').hidden = true;
  showAlert(alertBox, err.message);
});
