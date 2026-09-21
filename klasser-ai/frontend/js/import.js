/* Klasser AI - data import.
 *
 * Upload, review the proposed column mapping, then import. The upload step
 * writes nothing: it only parses the file and asks for a mapping, so a wrong
 * guess costs a correction rather than a bad import.
 */

const alertBox = document.getElementById('alert');
let job = null;      // the current upload response
let fields = {};     // available fields for the chosen import type
let schoolTz = 'Australia/Sydney';

document.getElementById('logout').addEventListener('click', logout);

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));
}

function show(id, visible) {
  document.getElementById(id).hidden = !visible;
}

/* --- Upload ---------------------------------------------------------------- */

document.getElementById('upload').addEventListener('click', async () => {
  hideAlert(alertBox);

  const input = document.getElementById('file');
  if (!input.files.length) {
    showAlert(alertBox, 'Choose a file first.');
    return;
  }

  const form = new FormData();
  form.append('file', input.files[0]);
  form.append('import_type', document.getElementById('import_type').value);

  const btn = document.getElementById('upload');
  btn.disabled = true;
  btn.textContent = 'Reading...';

  try {
    /* Multipart, so this bypasses apiCall's JSON handling. */
    const token = await accessToken();
    const res = await fetch(`${KLASSER.API_BASE}/import/upload`, {
      method: 'POST',
      headers: { Authorization: `Bearer ${token}` },
      body: form,
    });

    const payload = await res.json().catch(() => null);
    if (!res.ok) {
      const detail = payload && payload.detail;
      throw new Error(typeof detail === 'string' ? detail
        : `Upload failed (HTTP ${res.status})`);
    }

    job = payload;
    fields = payload.fields;
    renderMapping();
    show('step-map', true);
    show('step-done', false);
    document.getElementById('step-map').scrollIntoView({ behavior: 'smooth' });
  } catch (err) {
    showAlert(alertBox, err.message);
  } finally {
    btn.disabled = false;
    btn.textContent = 'Read file';
  }
});

/* --- Mapping review --------------------------------------------------------- */

function renderMapping() {
  document.getElementById('map-summary').textContent =
    `${job.rows_found} rows found in ${job.original_filename || 'your file'}. `
    + 'Check each column below, then import.';

  const warn = document.getElementById('map-warning');
  const notes = [...(job.warnings || []), ...(job.mapping.warnings || [])];
  if (job.mapping.source === 'heuristic') {
    notes.push('Columns were matched by name because no AI provider was '
      + 'available. Check them carefully.');
  }
  if (notes.length) {
    warn.className = 'alert alert-warning small';
    warn.innerHTML = notes.map(escapeHtml).join('<br>');
    warn.hidden = false;
  } else {
    warn.hidden = true;
  }

  const options = (selected) => {
    const opts = [`<option value="">(don't import)</option>`];
    for (const [name, meta] of Object.entries(fields)) {
      const req = meta.required ? ' *' : '';
      opts.push(
        `<option value="${name}" ${selected === name ? 'selected' : ''}>`
        + `${name}${req}</option>`
      );
    }
    return opts.join('');
  };

  document.getElementById('map-body').innerHTML =
    job.mapping.detected_columns.map((col, i) => {
      const samples = (col.sample_values || []).slice(0, 3)
        .map(escapeHtml).join(', ');
      const low = col.confidence === 'low' && col.klasser_field
        ? ' <span class="muted small">(unsure)</span>' : '';
      return `
        <tr>
          <td><strong>${escapeHtml(col.source_column)}</strong>${low}</td>
          <td class="muted">${samples || '<span class="muted">-</span>'}</td>
          <td>
            <select data-col="${i}" style="min-width:180px">
              ${options(col.klasser_field)}
            </select>
          </td>
        </tr>`;
    }).join('');
}

document.getElementById('cancel').addEventListener('click', () => {
  job = null;
  show('step-map', false);
  show('step-done', false);
  document.getElementById('file').value = '';
});

/* --- Import ------------------------------------------------------------------ */

document.getElementById('confirm').addEventListener('click', async () => {
  hideAlert(alertBox);

  const columns = Array.from(document.querySelectorAll('[data-col]')).map((sel) => ({
    source_column: job.mapping.detected_columns[Number(sel.dataset.col)].source_column,
    klasser_field: sel.value || null,
  }));

  const required = Object.entries(fields)
    .filter(([, meta]) => meta.required)
    .map(([name]) => name);
  const chosen = new Set(columns.map((c) => c.klasser_field).filter(Boolean));

  /* Teachers accept either the split names or one full name column. */
  const nameSatisfied = chosen.has('full_name')
    || (chosen.has('first_name') && chosen.has('surname'));
  const missing = required.filter((f) => {
    if (['first_name', 'surname'].includes(f)) return !nameSatisfied;
    return !chosen.has(f);
  });

  if (missing.length && !confirm(
    `These required fields are not mapped: ${missing.join(', ')}.\n\n`
    + 'Rows missing them will be skipped. Import anyway?')) {
    return;
  }

  const btn = document.getElementById('confirm');
  btn.disabled = true;
  btn.textContent = 'Importing...';

  try {
    const summary = await apiCall('POST', `/import/${job.job_id}/confirm`,
      { detected_columns: columns });
    renderResult(summary);
    show('step-map', false);
    show('step-done', true);
    await loadHistory();
    document.getElementById('step-done').scrollIntoView({ behavior: 'smooth' });
  } catch (err) {
    showAlert(alertBox, err.message);
  } finally {
    btn.disabled = false;
    btn.textContent = 'Import these rows';
  }
});

function renderResult(s) {
  const kind = s.failed > 0 ? 'warning' : 'success';
  document.getElementById('result-summary').innerHTML = `
    <div class="alert alert-${kind}">
      <strong>${s.inserted} added</strong>, ${s.updated} updated,
      ${s.skipped} skipped, ${s.failed} failed
      &mdash; out of ${s.rows_total} rows.
    </div>`;

  const box = document.getElementById('result-warnings');
  if (!s.warnings || !s.warnings.length) {
    box.innerHTML = '';
    return;
  }
  box.innerHTML = `
    <h3>Rows that did not import</h3>
    <div style="overflow-x:auto">
      <table class="table">
        <thead><tr><th>File row</th><th>Reason</th></tr></thead>
        <tbody>
          ${s.warnings.map((w) => `
            <tr><td>${w.row}</td><td>${escapeHtml(w.reason)}</td></tr>`).join('')}
        </tbody>
      </table>
    </div>`;
}

document.getElementById('again').addEventListener('click', () => {
  job = null;
  show('step-done', false);
  document.getElementById('file').value = '';
  document.getElementById('step-upload').scrollIntoView({ behavior: 'smooth' });
});

/* --- History ------------------------------------------------------------------- */

async function loadHistory() {
  const jobs = await apiCall('GET', '/import/jobs');
  document.getElementById('no-history').hidden = jobs.length > 0;

  document.getElementById('history').innerHTML = jobs.map((j) => `
    <tr>
      <td>${escapeHtml(j.original_filename)}</td>
      <td>${j.import_type}</td>
      <td>${j.status}</td>
      <td>${j.rows_imported ?? 0}</td>
      <td>${j.rows_skipped ?? 0}</td>
      <td>${j.rows_failed ?? 0}</td>
      <td class="muted">${formatLocalTime(j.created_at, schoolTz)}</td>
    </tr>`).join('');
}

/* --- Start ---------------------------------------------------------------------- */

(async () => {
  const me = await requireLogin();
  if (!me) return;

  schoolTz = me.timezone;
  document.getElementById('who').textContent =
    `${me.first_name} ${me.surname} - ${me.role}`;

  await loadHistory();

  document.getElementById('loading').hidden = true;
  document.getElementById('content').hidden = false;
})().catch((err) => {
  document.getElementById('loading').hidden = true;
  showAlert(alertBox, err.message);
});
