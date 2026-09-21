/* Klasser AI - raise and track support tickets. */

const alertBox = document.getElementById('alert');
const formAlert = document.getElementById('form-alert');

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

/* Mirrors triage() on the server, so the hint matches what actually happens.
   The server decides; this only sets expectations before sending. */
const URGENT = ['blocked', 'cannot generate', "can't generate", 'locked out',
  'urgent', 'not working at all'];

function expectedPriority() {
  const category = document.getElementById('category').value;
  const body = document.getElementById('body').value.toLowerCase();
  if (URGENT.some((p) => body.includes(p))) {
    return ['billing', 'generation'].includes(category) ? 'urgent' : 'high';
  }
  return ['billing', 'generation'].includes(category) ? 'high' : 'normal';
}

function updateHint() {
  const p = expectedPriority();
  document.getElementById('priority-hint').textContent =
    p === 'urgent' ? 'This will be treated as urgent.'
      : p === 'high' ? 'This will be prioritised.' : '';
}

document.getElementById('body').addEventListener('input', updateHint);
document.getElementById('category').addEventListener('change', updateHint);

/* --- Sending --------------------------------------------------------------- */

document.getElementById('submit').addEventListener('click', async () => {
  const subject = document.getElementById('subject').value.trim();
  const body = document.getElementById('body').value.trim();

  if (subject.length < 3) {
    showAlert(formAlert, 'Give the ticket a subject.');
    return;
  }
  if (body.length < 10) {
    showAlert(formAlert, 'Tell us a little more about what happened.');
    return;
  }

  const button = document.getElementById('submit');
  button.disabled = true;
  formAlert.hidden = true;

  try {
    const timetableId = document.getElementById('timetable').value;
    const result = await apiCall('POST', '/support/tickets', {
      category: document.getElementById('category').value,
      subject,
      body,
      timetable_id: timetableId || null,
    });

    showAlert(alertBox,
      `Ticket raised${result.priority === 'urgent' ? ' and marked urgent' : ''}.`,
      'success');
    document.getElementById('subject').value = '';
    document.getElementById('body').value = '';
    updateHint();
    await loadTickets();
  } catch (err) {
    showAlert(formAlert, err.message);
  } finally {
    button.disabled = false;
  }
});

/* --- Listing --------------------------------------------------------------- */

async function loadTickets() {
  const tickets = await apiCall('GET', '/support/tickets');
  const box = document.getElementById('tickets');

  if (!tickets.length) {
    box.innerHTML = '<p class="small muted">No tickets yet.</p>';
    return;
  }

  box.innerHTML = tickets.map((t) => `
    <div class="ticket">
      <div class="head">
        <span class="subject">${escapeHtml(t.subject)}</span>
        <span>
          <span class="chip ${escapeHtml(t.priority)}">${escapeHtml(t.priority)}</span>
          <span class="chip ${escapeHtml(t.status)}">${escapeHtml(t.status.replace('_', ' '))}</span>
        </span>
      </div>
      <div class="small muted">
        ${escapeHtml(t.category)} &middot; ${when(t.created_at)}
        &middot; ${escapeHtml(t.raised_by)}
        ${t.timetable_name ? `&middot; ${escapeHtml(t.timetable_name)}` : ''}
      </div>
      <div class="body">${escapeHtml(t.body)}</div>
      ${['open', 'in_progress'].includes(t.status)
        ? `<button class="btn small" data-close="${t.id}"
                   style="margin-top:10px">Withdraw</button>` : ''}
    </div>`).join('');

  document.querySelectorAll('[data-close]').forEach((btn) => {
    btn.addEventListener('click', async () => {
      if (!confirm('Withdraw this ticket?')) return;
      try {
        await apiCall('POST', `/support/tickets/${btn.dataset.close}/close`);
        await loadTickets();
      } catch (err) {
        showAlert(alertBox, err.message);
      }
    });
  });
}

/* --- Load ------------------------------------------------------------------ */

(async () => {
  const me = await requireLogin();
  if (!me) return;

  document.getElementById('who').textContent =
    `${me.first_name} ${me.surname} - ${me.role}`;

  const [timetables, contact] = await Promise.all([
    apiCall('GET', '/timetables'),
    apiCall('GET', '/support/contact'),
  ]);

  document.getElementById('timetable').innerHTML =
    '<option value="">None</option>'
    + timetables.map((t) =>
      `<option value="${t.id}">${escapeHtml(t.name)}</option>`).join('');

  document.getElementById('contact').innerHTML =
    `Email <a href="mailto:${escapeHtml(contact.support_email)}">`
    + `${escapeHtml(contact.support_email)}</a> &middot; response `
    + `${escapeHtml(contact.response_time)}.<br>`
    + `For urgent billing issues, <a href="mailto:${escapeHtml(contact.billing_email)}">`
    + `${escapeHtml(contact.billing_email)}</a>.`;

  await loadTickets();
  updateHint();

  document.getElementById('loading').hidden = true;
  document.getElementById('content').hidden = false;
})().catch((err) => {
  document.getElementById('loading').hidden = true;
  showAlert(alertBox, err.message);
});
