/* Klasser - school data pages.
 *
 * One table driver for all four entity types. Each type declares its columns,
 * its form fields and how to turn a row into a payload; everything else -
 * loading, searching, the dialog, save and delete - is shared.
 */

const alertBox = document.getElementById('alert');
const dialog = document.getElementById('dialog');
const dialogAlert = document.getElementById('dialog-alert');

let campuses = [];
let current = 'teachers';
let rows = [];
let editing = null;

document.getElementById('logout').addEventListener('click', logout);

/* --- Type definitions ----------------------------------------------------- */

const campusOptions = (allowBlank) => [
  ...(allowBlank ? [{ value: '', label: '(none)' }] : []),
  ...campuses.map((c) => ({ value: c.id, label: c.name })),
];

const TYPES = {
  teachers: {
    title: 'Teachers',
    path: '/schools/teachers',
    columns: [
      ['full_name', 'Name'],
      ['subjects', 'Subjects', (v) => (v || []).join(', ')],
      ['campus_name', 'Campus'],
      ['max_blocks', 'Max blocks'],
      ['max_duties_per_cycle', 'Max duties'],
      ['duty_exempt', 'Duty exempt', (v) => (v ? 'Yes' : '')],
    ],
    fields: () => [
      { key: 'first_name', label: 'First name', required: true },
      { key: 'surname', label: 'Surname', required: true },
      { key: 'subjects', label: 'Subjects', hint: 'Comma separated', list: true },
      { key: 'campus_id', label: 'Campus', options: campusOptions(true) },
      { key: 'max_blocks', label: 'Max blocks per day', type: 'number' },
      { key: 'max_duties_per_cycle', label: 'Max duties per cycle', type: 'number' },
      { key: 'gender', label: 'Gender' },
      { key: 'duty_exempt', label: 'Exempt from all duties', type: 'checkbox' },
    ],
    search: (r, q) => r.full_name.toLowerCase().includes(q),

    /* duty_exempt is a row in teacher_duty_exemptions, not a teacher column, so
     * it is saved through its own endpoint after the teacher itself. */
    afterSave: async (id, payload) => {
      await apiCall('PUT',
        `/schools/teachers/${id}/duty-exempt?exempt=${payload.duty_exempt}`);
    },

    /* Strip it from the teacher payload - the API would reject the extra key. */
    clean: (payload) => {
      const { duty_exempt, ...rest } = payload;
      return rest;
    },
  },

  students: {
    title: 'Students',
    path: '/schools/students',
    paged: true,
    columns: [
      ['full_name', 'Name'],
      ['year_group', 'Year'],
      ['campus_name', 'Campus'],
      ['subjects', 'Subjects', (v) => (v || []).join(', ')],
      ['gender', 'Gender'],
      ['ability_band', 'Band'],
    ],
    fields: () => [
      { key: 'full_name', label: 'Full name', required: true },
      { key: 'year_group', label: 'Year group', required: true, hint: 'e.g. Year 11' },
      { key: 'subjects', label: 'Subjects', hint: 'Comma separated', list: true },
      { key: 'campus_id', label: 'Campus', options: campusOptions(true) },
      { key: 'gender', label: 'Gender' },
      { key: 'ability_band', label: 'Ability band', hint: 'high / mid / low' },
    ],
  },

  rooms: {
    title: 'Rooms',
    path: '/schools/rooms',
    columns: [
      ['name', 'Name'],
      ['campus_name', 'Campus'],
      ['capacity', 'Capacity'],
      ['preferred_min_capacity', 'Preferred min'],
      ['room_type', 'Type'],
      ['allows_split', 'Splittable', (v) => (v ? 'Yes' : '')],
    ],
    fields: () => [
      { key: 'name', label: 'Room name', required: true },
      { key: 'campus_id', label: 'Campus', required: true, options: campusOptions(false) },
      { key: 'capacity', label: 'Capacity', type: 'number', required: true },
      { key: 'preferred_min_capacity', label: 'Preferred minimum', type: 'number' },
      { key: 'room_type', label: 'Room type', hint: 'classroom, computer_lab, science_lab, gym' },
      { key: 'allows_split', label: 'Allows split classes', type: 'checkbox' },
    ],
    search: (r, q) => r.name.toLowerCase().includes(q),
  },

  subjects: {
    title: 'Subjects',
    path: '/schools/subjects',
    columns: [
      ['subject', 'Subject'],
      ['year_level', 'Year level', (v) => v || 'All'],
      ['periods', 'Periods/cycle', (_, r) => {
        const lo = r.min_periods_per_cycle;
        const hi = r.max_periods_per_cycle;
        if (lo == null && hi == null) return 'default';
        if (lo != null && hi != null) return lo === hi ? `${lo}` : `${lo}-${hi}`;
        return lo != null ? `${lo}+` : `up to ${hi}`;
      }],
      ['hard_max_size', 'Hard max'],
      ['soft_max_size', 'Soft max'],
      ['min_size', 'Min'],
      ['default_room_type', 'Room type'],
      ['campus_locked_name', 'Locked to'],
      ['is_double_period', 'Double', (v) => (v ? 'Yes' : '')],
      ['code_prefix', 'Prefix'],
    ],
    fields: () => [
      { key: 'subject', label: 'Subject', required: true },
      { key: 'year_level', label: 'Year level', hint: 'Blank means all year levels' },
      { key: 'min_periods_per_cycle', label: 'Minimum periods per cycle',
        type: 'number',
        hint: 'How often a class must meet. Blank uses the school default.' },
      { key: 'max_periods_per_cycle', label: 'Maximum periods per cycle',
        type: 'number',
        hint: 'The allocator may go up to this where the timetable allows.' },
      { key: 'hard_max_size', label: 'Hard max size', type: 'number' },
      { key: 'soft_max_size', label: 'Soft max size', type: 'number' },
      { key: 'min_size', label: 'Minimum size', type: 'number' },
      { key: 'default_room_type', label: 'Required room type' },
      { key: 'campus_locked_id', label: 'Lock to campus', options: campusOptions(true) },
      { key: 'is_double_period', label: 'Runs as a double period', type: 'checkbox' },
      { key: 'code_prefix', label: 'Class code prefix', hint: 'e.g. COM' },
    ],
    search: (r, q) => r.subject.toLowerCase().includes(q),
  },
};

/* --- Loading and rendering ------------------------------------------------- */

async function load() {
  const type = TYPES[current];
  const search = document.getElementById('search').value.trim();

  let data;
  if (type.paged) {
    const qs = search ? `?search=${encodeURIComponent(search)}` : '';
    data = await apiCall('GET', type.path + qs);
    rows = data.students;
    document.getElementById('count').textContent = `${data.total} total`;
  } else {
    rows = await apiCall('GET', type.path);
    const q = search.toLowerCase();
    if (q && type.search) rows = rows.filter((r) => type.search(r, q));
    document.getElementById('count').textContent = `${rows.length} shown`;
  }

  document.getElementById('head').innerHTML =
    type.columns.map(([, label]) => `<th>${label}</th>`).join('') + '<th></th>';

  document.getElementById('body').innerHTML = rows.map((r, i) => {
    /* Formatters get the whole row too, for columns derived from more than one
     * field (periods per cycle is a min and a max). */
    const cells = type.columns.map(([key, , fmt]) => {
      const raw = r[key];
      const value = fmt ? fmt(raw, r) : (raw === null || raw === undefined ? '' : raw);
      return `<td>${escapeHtml(String(value))}</td>`;
    }).join('');
    return `<tr>${cells}<td><button class="btn small" data-edit="${i}">Edit</button></td></tr>`;
  }).join('');

  const empty = document.getElementById('empty');
  empty.hidden = rows.length > 0;
  empty.textContent = search
    ? 'Nothing matches that search.'
    : `No ${type.title.toLowerCase()} yet. Use Add to create one.`;

  document.querySelectorAll('[data-edit]').forEach((btn) => {
    btn.addEventListener('click', () => openDialog(rows[Number(btn.dataset.edit)]));
  });
}

function escapeHtml(s) {
  return s.replace(/[&<>"']/g, (c) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));
}

/* --- Dialog ---------------------------------------------------------------- */

function openDialog(row) {
  editing = row || null;
  const type = TYPES[current];
  dialogAlert.hidden = true;

  document.getElementById('dialog-title').textContent =
    (row ? 'Edit ' : 'Add ') + type.title.toLowerCase().replace(/s$/, '');
  document.getElementById('dialog-delete').hidden = !row;

  document.getElementById('dialog-fields').innerHTML = type.fields().map((f) => {
    const value = row ? row[f.key] : '';
    const id = `f_${f.key}`;
    const hint = f.hint ? `<div class="hint">${f.hint}</div>` : '';

    if (f.options) {
      const opts = f.options.map((o) =>
        `<option value="${o.value}" ${String(value || '') === o.value ? 'selected' : ''}>${o.label}</option>`
      ).join('');
      return `<div class="field"><label for="${id}">${f.label}</label>
              <select id="${id}">${opts}</select>${hint}</div>`;
    }
    if (f.type === 'checkbox') {
      return `<div class="field" style="display:flex;gap:8px;align-items:center">
              <input type="checkbox" id="${id}" ${value ? 'checked' : ''} style="width:auto">
              <label for="${id}" style="margin:0">${f.label}</label></div>`;
    }
    const shown = f.list ? (value || []).join(', ') : (value ?? '');
    return `<div class="field"><label for="${id}">${f.label}</label>
            <input id="${id}" type="${f.type || 'text'}" value="${escapeHtml(String(shown))}">
            ${hint}</div>`;
  }).join('');

  dialog.showModal();
}

function readDialog() {
  const payload = {};
  for (const f of TYPES[current].fields()) {
    const el = document.getElementById(`f_${f.key}`);
    if (f.type === 'checkbox') payload[f.key] = el.checked;
    else if (f.list) {
      payload[f.key] = el.value.split(',').map((s) => s.trim()).filter(Boolean);
    } else if (f.type === 'number') {
      payload[f.key] = el.value === '' ? null : Number(el.value);
    } else {
      payload[f.key] = el.value.trim() === '' ? null : el.value.trim();
    }
    if (f.required && (payload[f.key] === null || payload[f.key] === '')) {
      throw new Error(`${f.label} is required.`);
    }
  }
  return payload;
}

document.getElementById('dialog-cancel')
  .addEventListener('click', () => dialog.close());

/* Bound to submit, not click, so Enter in a field saves instead of closing the
 * dialog and losing the edit. */
document.getElementById('dialog-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  dialogAlert.hidden = true;

  const type = TYPES[current];
  let payload;
  try {
    payload = readDialog();
  } catch (err) {
    showAlert(dialogAlert, err.message);
    return;
  }

  const btn = document.getElementById('dialog-save');
  btn.disabled = true;
  btn.textContent = 'Saving...';
  try {
    const body = type.clean ? type.clean(payload) : payload;

    let id = editing ? editing.id : null;
    if (editing) {
      await apiCall('PATCH', `${type.path}/${editing.id}`, body);
    } else {
      const created = await apiCall('POST', type.path, body);
      id = created && created.id;
    }

    /* Some settings live on their own endpoint rather than the main row. */
    if (type.afterSave && id) await type.afterSave(id, payload);

    dialog.close();
    await load();
    showAlert(alertBox, 'Saved.', 'success');
  } catch (err) {
    showAlert(dialogAlert, err.message);
  } finally {
    btn.disabled = false;
    btn.textContent = 'Save';
  }
});

document.getElementById('dialog-delete').addEventListener('click', async () => {
  if (!editing) return;
  if (!confirm('Delete this permanently?')) return;

  try {
    await apiCall('DELETE', `${TYPES[current].path}/${editing.id}`);
    dialog.close();
    await load();
    showAlert(alertBox, 'Deleted.', 'success');
  } catch (err) {
    // The delete guard explains what is still referencing the row.
    showAlert(dialogAlert, err.message);
  }
});

/* --- Tabs and search -------------------------------------------------------- */

document.querySelectorAll('.tab').forEach((tab) => {
  tab.addEventListener('click', async () => {
    document.querySelectorAll('.tab').forEach((t) => t.classList.remove('active'));
    tab.classList.add('active');
    current = tab.dataset.tab;
    document.getElementById('title').textContent = TYPES[current].title;
    document.getElementById('search').value = '';
    hideAlert(alertBox);
    await load();
  });
});

let searchTimer;
document.getElementById('search').addEventListener('input', () => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(load, 250);
});

document.getElementById('add').addEventListener('click', () => openDialog(null));

/* --- Start ------------------------------------------------------------------ */

(async () => {
  const me = await requireLogin();
  if (!me) return;

  document.getElementById('who').textContent =
    `${me.first_name} ${me.surname} - ${me.role}`;

  campuses = await apiCall('GET', '/schools/campuses');
  await load();

  document.getElementById('loading').hidden = true;
  document.getElementById('content').hidden = false;
})().catch((err) => {
  document.getElementById('loading').hidden = true;
  showAlert(alertBox, err.message);
});
