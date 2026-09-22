/* Klasser - timetable output.
 *
 * Four grid views (class, teacher, room, student) plus transport and duties.
 * The server groups and filters; this page only lays out what it is given, so
 * a large cycle never ships whole to render one teacher's week.
 */

const alertBox = document.getElementById('alert');
const params = new URLSearchParams(window.location.search);
const versionId = params.get('version');

let summary = null;
let view = 'class';
let options = [];

document.getElementById('logout').addEventListener('click', logout);

function escapeHtml(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));
}

/* Subject colouring follows BRAND.md's timetable grid palette. */
function subjectClass(subject) {
  const s = (subject || '').toLowerCase();
  if (/(math)/.test(s)) return 'subj-maths';
  if (/(science|physic|chem|bio)/.test(s)) return 'subj-science';
  if (/(english|history|geograph|human)/.test(s)) return 'subj-english';
  if (/(comput|digital|it)/.test(s)) return 'subj-default';
  return 'subj-other';
}

/* --- Grid ------------------------------------------------------------------ */

function renderGrid(data) {
  const days = summary.days;
  const periods = summary.periods;
  const labels = summary.period_labels || {};

  if (!days.length || !periods.length) {
    document.getElementById('grid-wrap').innerHTML =
      '<p class="muted">This timetable has no teaching periods.</p>';
    return;
  }

  const header = ['<th></th>']
    .concat(days.map((d) => `<th>Day ${d}</th>`))
    .join('');

  const rows = periods.map((p) => {
    const cells = days.map((d) => {
      const cell = data.cells[`${d}-${p}`];
      if (!cell) return '<td><span class="empty">-</span></td>';

      /* What matters in a cell depends on the view: a teacher looking at their
       * own week does not need their own name repeated in every box. */
      const lines = {
        class: [cell.teacher, `${cell.room} - ${cell.students} students`],
        teacher: [cell.class_code, `${cell.room} - ${cell.students}`],
        room: [cell.class_code, cell.teacher],
        student: [cell.teacher, cell.room],
      }[view] || [cell.teacher, cell.room];

      return `
        <td>
          <div class="cell ${subjectClass(cell.subject)}">
            <strong>${escapeHtml(view === 'class' ? cell.subject : cell.class_code)}</strong>
            ${lines.map((l) => `<span>${escapeHtml(l)}</span>`).join('')}
          </div>
        </td>`;
    }).join('');

    return `<tr><th>${escapeHtml(labels[String(p)] || `P${p}`)}</th>${cells}</tr>`;
  }).join('');

  document.getElementById('grid-wrap').innerHTML =
    `<table class="grid"><thead><tr>${header}</tr></thead><tbody>${rows}</tbody></table>`;

  document.getElementById('count').textContent =
    `${data.count} period${data.count === 1 ? '' : 's'}`;
}

function renderList(rows, columns) {
  document.getElementById('picker').hidden = true;
  if (!rows.length) {
    document.getElementById('grid-wrap').innerHTML =
      '<p class="muted">Nothing to show for this timetable.</p>';
    return;
  }
  const head = columns.map(([, label]) => `<th>${label}</th>`).join('');
  const body = rows.map((r) => `
    <tr>${columns.map(([key, , fmt]) =>
      `<td>${escapeHtml(fmt ? fmt(r[key], r) : r[key])}</td>`).join('')}</tr>`
  ).join('');

  document.getElementById('grid-wrap').innerHTML =
    `<table class="table"><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table>`;
  document.getElementById('count').textContent = `${rows.length} rows`;
}

/* --- Loading --------------------------------------------------------------- */

async function loadView() {
  hideAlert(alertBox);
  document.getElementById('grid-wrap').innerHTML =
    '<p class="muted">Loading...</p>';

  if (view === 'transport') {
    const rows = await apiCall('GET', `/timetables/version/${versionId}/transport`);
    renderList(rows, [
      ['day_number', 'Day'], ['departure', 'Departs'], ['arrival', 'Arrives'],
      ['bus', 'Bus'], ['from_campus', 'From'], ['to_campus', 'To'],
      ['passenger_count', 'Passengers'],
      ['is_empty_leg', 'Empty leg', (v) => (v ? 'Yes' : '')],
    ]);
    return;
  }

  if (view === 'duties') {
    const rows = await apiCall('GET', `/timetables/version/${versionId}/duties`);
    renderList(rows, [
      ['day_number', 'Day'], ['duty', 'Duty'], ['timing', 'When'],
      ['start_time', 'From'], ['end_time', 'To'],
      ['teacher', 'Teacher'], ['campus', 'Campus'],
    ]);
    return;
  }

  document.getElementById('picker').hidden = false;
  options = await apiCall('GET',
    `/timetables/version/${versionId}/options?view=${view}`);

  applyFilter();
  if (!options.length) {
    document.getElementById('grid-wrap').innerHTML =
      '<p class="muted">Nothing to show for this view.</p>';
    return;
  }
  await loadGrid();
}

function applyFilter() {
  const term = document.getElementById('filter').value.trim().toLowerCase();
  const shown = term
    ? options.filter((o) => o.label.toLowerCase().includes(term))
    : options;

  document.getElementById('key').innerHTML = shown.map((o) =>
    `<option value="${escapeHtml(o.id)}">${escapeHtml(o.label)}`
    + `${o.detail ? ` - ${escapeHtml(o.detail)}` : ''}</option>`).join('');
}

async function loadGrid() {
  const key = document.getElementById('key').value;
  if (!key) return;
  const data = await apiCall('GET',
    `/timetables/version/${versionId}/grid?view=${view}&key=${encodeURIComponent(key)}`);
  renderGrid(data);
}

/* --- Events ---------------------------------------------------------------- */

document.querySelectorAll('.tab').forEach((tab) => {
  tab.addEventListener('click', async () => {
    document.querySelectorAll('.tab').forEach((t) => t.classList.remove('active'));
    tab.classList.add('active');
    view = tab.dataset.view;
    document.getElementById('filter').value = '';
    try {
      await loadView();
    } catch (err) {
      showAlert(alertBox, err.message);
    }
  });
});

document.getElementById('key').addEventListener('change', () => {
  loadGrid().catch((err) => showAlert(alertBox, err.message));
});

let filterTimer;
document.getElementById('filter').addEventListener('input', () => {
  clearTimeout(filterTimer);
  filterTimer = setTimeout(() => {
    applyFilter();
    loadGrid().catch(() => {});
  }, 200);
});

document.getElementById('publish').addEventListener('click', async () => {
  if (!confirm('Publish this version? It becomes the live timetable and the '
               + 'current published version is archived.')) return;
  try {
    const res = await apiCall('POST',
      `/timetables/version/${versionId}/publish`);
    showAlert(alertBox, `Version ${res.version_number} published.`, 'success');
    document.getElementById('publish').hidden = true;
  } catch (err) {
    showAlert(alertBox, err.message);
  }
});

/* --- Start ----------------------------------------------------------------- */

(async () => {
  const me = await requireLogin();
  if (!me) return;

  document.getElementById('who').textContent =
    `${me.first_name} ${me.surname} - ${me.role}`;

  if (!versionId) {
    showAlert(alertBox, 'No timetable version specified.');
    document.getElementById('loading').hidden = true;
    return;
  }

  summary = await apiCall('GET', `/timetables/version/${versionId}/summary`);

  document.getElementById('title').textContent = summary.name;
  document.getElementById('subtitle').textContent =
    `Version ${summary.version_number} - ${summary.status}`
    + (summary.published_at
        ? ` - published ${formatLocalTime(summary.published_at, me.timezone)}`
        : '');

  const c = summary.counts;
  document.getElementById('stats').innerHTML = [
    ['Classes', c.classes], ['Periods timetabled', c.entries],
    ['Teachers', c.teachers], ['Rooms', c.rooms],
    ['Students', c.students], ['Duties', c.duties],
  ].map(([label, value]) => `
      <div class="card" style="padding:12px">
        <div style="font-size:22px;font-weight:700">${value}</div>
        <div class="small muted">${label}</div>
      </div>`).join('');

  // A sample belongs to the demonstration school. The server refuses to
  // publish or edit it, so the page must not offer either - an enabled
  // button that always errors is worse than no button.
  if (summary.is_sample) {
    document.getElementById('sample-banner').hidden = false;
  } else if (summary.status === 'draft' && ['owner', 'dev'].includes(me.role)) {
    document.getElementById('publish').hidden = false;
    // Only a draft can be edited - a published timetable is what the school is
    // running on, so changes to it go through a new version.
    const edit = document.getElementById('edit-link');
    edit.href = `edit.html?version=${versionId}`;
    edit.hidden = me.role !== 'owner';
  }
  // The versions list is scoped to the viewer's own school, so for a sample it
  // would 404. Send them back to the dashboard instead.
  const link = document.getElementById('versions-link');
  link.href = summary.is_sample
    ? 'dashboard.html'
    : `timetables.html?timetable=${summary.timetable_id}`;
  link.textContent = summary.is_sample ? 'Back to dashboard' : 'Versions';
  link.hidden = false;

  await loadView();

  document.getElementById('loading').hidden = true;
  document.getElementById('content').hidden = false;
})().catch((err) => {
  document.getElementById('loading').hidden = true;
  showAlert(alertBox, err.message);
});
