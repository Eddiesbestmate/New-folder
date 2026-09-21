/* Klasser AI - define, preview and download CSV export templates. */

const alertBox = document.getElementById('alert');
const editorAlert = document.getElementById('editor-alert');

let templates = [];
let fields = [];
let formats = [];
let editing = null;      // the template being edited, or null for a new one
let columns = [];
let versions = [];

document.getElementById('logout').addEventListener('click', logout);

function escapeHtml(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));
}

/* --- Columns --------------------------------------------------------------- */

function fieldOptions(selected) {
  return fields.map((f) =>
    `<option value="${escapeHtml(f.source)}" ${f.source === selected ? 'selected' : ''}>`
    + `${escapeHtml(f.source)} - ${escapeHtml(f.description)}</option>`).join('');
}

function formatOptions(selected) {
  return `<option value="">plain</option>`
    + formats.map((f) =>
      `<option value="${escapeHtml(f)}" ${f === selected ? 'selected' : ''}>`
      + `${escapeHtml(f)}</option>`).join('');
}

function renderColumns() {
  document.getElementById('columns').innerHTML = columns.map((col, i) => `
    <div class="col-row">
      <input data-label="${i}" value="${escapeHtml(col.label)}" maxlength="100">
      <select data-source="${i}">${fieldOptions(col.source)}</select>
      <select data-format="${i}">${formatOptions(col.format)}</select>
      <button class="remove" data-remove="${i}" type="button"
              title="Remove">&times;</button>
    </div>`).join('') || '<p class="small muted">No columns yet.</p>';

  document.querySelectorAll('[data-label]').forEach((el) => {
    el.addEventListener('input', () => { columns[el.dataset.label].label = el.value; });
  });
  document.querySelectorAll('[data-source]').forEach((el) => {
    el.addEventListener('change', () => { columns[el.dataset.source].source = el.value; });
  });
  document.querySelectorAll('[data-format]').forEach((el) => {
    el.addEventListener('change', () => {
      columns[el.dataset.format].format = el.value || null;
    });
  });
  document.querySelectorAll('[data-remove]').forEach((el) => {
    el.addEventListener('click', () => {
      columns.splice(Number(el.dataset.remove), 1);
      renderColumns();
    });
  });
}

document.getElementById('add-column').addEventListener('click', () => {
  if (!fields.length) return;
  columns.push({ label: fields[0].source, source: fields[0].source, format: null });
  renderColumns();
});

/* --- Field vocabulary ------------------------------------------------------ */

async function loadFields() {
  const outputType = document.getElementById('t-type').value;
  const data = await apiCall('GET', '/export/fields', null,
    { output_type: outputType });
  fields = data.fields;
  formats = data.formats;

  document.getElementById('t-delimiter').innerHTML = data.delimiters.map((d) =>
    `<option value="${escapeHtml(d.value)}">${escapeHtml(d.label)}</option>`).join('');

  renderColumns();
}

document.getElementById('t-type').addEventListener('change', async () => {
  // Fields differ per output type, so existing columns no longer apply.
  if (columns.length
      && !confirm('Changing what to export clears the columns. Continue?')) {
    return;
  }
  columns = [];
  await loadFields();
});

/* --- Interpreting a description -------------------------------------------- */

document.getElementById('interpret').addEventListener('click', async () => {
  const description = document.getElementById('describe').value.trim();
  if (description.length < 2) {
    showAlert(editorAlert, 'Paste a header row or describe the columns you need.');
    return;
  }

  const button = document.getElementById('interpret');
  button.disabled = true;
  document.getElementById('interpret-note').textContent = 'Working...';

  try {
    const result = await apiCall('POST', '/export/interpret', {
      description,
      output_type: document.getElementById('t-type').value,
    });

    columns = result.columns;
    renderColumns();

    const parts = [];
    parts.push(result.used_ai ? 'Interpreted by AI.' : 'Matched by name, no AI needed.');
    if (result.unmatched.length) {
      parts.push(`Could not match: ${result.unmatched.join(', ')}.`);
    }
    if (result.note) parts.push(result.note);
    document.getElementById('interpret-note').textContent = parts.join(' ');
  } catch (err) {
    document.getElementById('interpret-note').textContent = '';
    showAlert(editorAlert, err.message);
  } finally {
    button.disabled = false;
  }
});

/* --- Editor ---------------------------------------------------------------- */

async function openEditor(template) {
  editing = template;
  editorAlert.hidden = true;
  document.getElementById('editor').hidden = false;
  document.getElementById('editor-title').textContent =
    template ? `Edit "${template.name}"` : 'New template';
  document.getElementById('delete').hidden = !template;
  document.getElementById('interpret-note').textContent = '';
  document.getElementById('describe').value = '';

  document.getElementById('t-name').value = template ? template.name : '';
  document.getElementById('t-type').value =
    template ? template.output_type : 'timetable_entries';
  document.getElementById('t-header').checked =
    template ? template.include_header : true;

  columns = template ? template.columns.map((c) => ({ ...c })) : [];
  await loadFields();
  if (template) document.getElementById('t-delimiter').value = template.delimiter;

  document.getElementById('preview-card').hidden = !template;
  if (template) await refreshPreview();

  document.getElementById('editor').scrollIntoView({ behavior: 'smooth' });
}

document.getElementById('new').addEventListener('click', () => openEditor(null));
document.getElementById('cancel').addEventListener('click', () => {
  document.getElementById('editor').hidden = true;
  document.getElementById('preview-card').hidden = true;
  editing = null;
});

document.getElementById('save').addEventListener('click', async () => {
  const name = document.getElementById('t-name').value.trim();
  if (!name) {
    showAlert(editorAlert, 'Give the template a name.');
    return;
  }
  if (!columns.length) {
    showAlert(editorAlert, 'Add at least one column.');
    return;
  }

  const body = {
    name,
    output_type: document.getElementById('t-type').value,
    columns,
    delimiter: document.getElementById('t-delimiter').value,
    include_header: document.getElementById('t-header').checked,
  };

  const button = document.getElementById('save');
  button.disabled = true;
  try {
    if (editing) {
      await apiCall('PUT', `/export/templates/${editing.id}`, body);
      showAlert(alertBox, 'Template saved.', 'success');
    } else {
      const created = await apiCall('POST', '/export/templates', body);
      showAlert(alertBox, 'Template created.', 'success');
      editing = { id: created.id, ...body };
    }
    await loadTemplates();
    document.getElementById('preview-card').hidden = false;
    await refreshPreview();
  } catch (err) {
    showAlert(editorAlert, err.message);
  } finally {
    button.disabled = false;
  }
});

document.getElementById('delete').addEventListener('click', async () => {
  if (!editing || !confirm(`Delete "${editing.name}"?`)) return;
  try {
    await apiCall('DELETE', `/export/templates/${editing.id}`);
    showAlert(alertBox, 'Template deleted.', 'success');
    document.getElementById('editor').hidden = true;
    document.getElementById('preview-card').hidden = true;
    editing = null;
    await loadTemplates();
  } catch (err) {
    showAlert(alertBox, err.message);
  }
});

/* --- Preview and download -------------------------------------------------- */

async function refreshPreview() {
  const versionId = document.getElementById('p-version').value;
  if (!editing || !versionId) {
    document.getElementById('preview').textContent =
      'Generate a timetable first - there is nothing to preview yet.';
    document.getElementById('download').disabled = true;
    return;
  }

  try {
    const data = await apiCall(
      'GET', `/export/preview/${versionId}/${editing.id}`, null, { rows: 5 });
    document.getElementById('preview-meta').textContent =
      `${data.total_rows} rows in full, showing ${data.showing}.`;
    document.getElementById('preview').textContent = data.lines.join('\n');
    document.getElementById('download').disabled = data.total_rows === 0;
  } catch (err) {
    document.getElementById('preview').textContent = err.message;
    document.getElementById('download').disabled = true;
  }
}

document.getElementById('p-version').addEventListener('change', refreshPreview);

document.getElementById('download').addEventListener('click', async () => {
  const versionId = document.getElementById('p-version').value;
  if (!editing || !versionId) return;

  // Fetched with the auth header and turned into a blob: a plain link cannot
  // carry the token, and putting it in the query string would log it.
  try {
    const token = await accessToken();
    const response = await fetch(
      `${KLASSER.API_BASE}/export/download/${versionId}/${editing.id}`,
      { headers: { Authorization: `Bearer ${token}` } });
    if (!response.ok) throw new Error(`Export failed (${response.status})`);

    const disposition = response.headers.get('Content-Disposition') || '';
    const match = disposition.match(/filename="([^"]+)"/);
    const blob = await response.blob();

    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = match ? match[1] : 'export.csv';
    document.body.appendChild(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(url);
  } catch (err) {
    showAlert(alertBox, err.message);
  }
});

/* --- Templates list -------------------------------------------------------- */

async function loadTemplates() {
  templates = await apiCall('GET', '/export/templates');
  const box = document.getElementById('templates');

  if (!templates.length) {
    box.innerHTML = '<p class="small muted">No templates yet. '
      + 'Create one to export your timetable.</p>';
    return;
  }

  box.innerHTML = templates.map((t) => `
    <div class="tmpl ${editing && editing.id === t.id ? 'active' : ''}">
      <div style="display:flex;justify-content:space-between;gap:10px">
        <span style="font-weight:600">${escapeHtml(t.name)}</span>
        <button class="btn small" data-edit="${t.id}">Open</button>
      </div>
      <div class="small muted">
        ${escapeHtml(t.output_type.replace('_', ' '))} &middot;
        ${t.columns.length} columns &middot;
        ${t.include_header ? 'with header' : 'no header'}
      </div>
    </div>`).join('');

  document.querySelectorAll('[data-edit]').forEach((btn) => {
    btn.addEventListener('click', () => {
      const t = templates.find((x) => x.id === btn.dataset.edit);
      openEditor(t).catch((err) => showAlert(alertBox, err.message));
    });
  });
}

/* --- Load ------------------------------------------------------------------ */

(async () => {
  const me = await requireLogin();
  if (!me) return;

  document.getElementById('who').textContent =
    `${me.first_name} ${me.surname} - ${me.role}`;

  // Every version across every timetable, so a template can be previewed
  // against whichever one the school cares about.
  const list = await apiCall('GET', '/timetables');
  versions = [];
  for (const t of list) {
    if (!t.versions) continue;
    const rows = await apiCall('GET', `/timetables/${t.id}/versions`);
    rows.forEach((v) => versions.push(
      { id: v.id, label: `${t.name} - v${v.version_number} (${v.status})` }));
  }

  document.getElementById('p-version').innerHTML = versions.length
    ? versions.map((v) =>
      `<option value="${v.id}">${escapeHtml(v.label)}</option>`).join('')
    : '<option value="">No timetables generated yet</option>';

  await loadTemplates();

  if (me.role !== 'owner') {
    document.getElementById('new').disabled = true;
  }

  document.getElementById('loading').hidden = true;
  document.getElementById('content').hidden = false;
})().catch((err) => {
  document.getElementById('loading').hidden = true;
  showAlert(alertBox, err.message);
});
