/* Klasser - plain-English class rules.
 *
 * The backend has done this work since the intake router was written; it just
 * had no screen. The shape of the page follows INTAKE's rule that a guess is
 * never presented as a statement: fields the AI filled in are marked, and
 * correcting one clears the mark.
 */

const alertBox = document.getElementById('alert');

let current = null;   // the rule set on screen
let canEdit = false;  // owners only; everyone else reads

document.getElementById('logout').addEventListener('click', logout);

const FORMATIONS = ['explicit', 'rule', 'mixed', 'raw_list'];

function escapeHtml(s) {
  return String(s ?? '').replace(/[&<>"']/g, (c) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));
}

/* --- Review table ---------------------------------------------------------- */

function renderGroups(groups) {
  const body = document.getElementById('groups');
  body.innerHTML = groups.map((g, i) => {
    const inferred = new Set(g.inferred || []);
    const mark = (field) => (inferred.has(field) ? ' class="inferred"' : '');
    const val = (field) => escapeHtml(g[field] ?? '');
    return `
      <tr class="group-row" data-index="${i}">
        <td${mark('subject')}><input data-field="subject" value="${val('subject')}"></td>
        <td${mark('year_level')}><input data-field="year_level" value="${val('year_level')}"></td>
        <td${mark('class_count')}><input data-field="class_count" type="number" min="1"
             value="${val('class_count')}"></td>
        <td${mark('max_size')}><input data-field="max_size" type="number" min="1"
             value="${val('max_size')}"></td>
        <td${mark('room_type')}><input data-field="room_type" value="${val('room_type')}"></td>
        <td${mark('is_double')}>
          <input data-field="is_double" type="checkbox" ${g.is_double ? 'checked' : ''}>
        </td>
        <td${mark('notes')}><input data-field="notes" value="${val('notes')}"></td>
      </tr>`;
  }).join('');

  // Editing a field means the school has now said it, so drop the shading.
  body.querySelectorAll('input').forEach((input) => {
    input.disabled = !canEdit || (current && current.confirmed);
    input.addEventListener('input', () => {
      input.closest('td').classList.remove('inferred');
    });
  });
}

function readGroups() {
  return Array.from(document.querySelectorAll('#groups .group-row')).map((row) => {
    const original = current.groups[Number(row.dataset.index)] || {};
    const out = { ...original };
    row.querySelectorAll('input').forEach((input) => {
      const field = input.dataset.field;
      if (input.type === 'checkbox') out[field] = input.checked;
      else if (input.type === 'number') {
        out[field] = input.value === '' ? null : Number(input.value);
      } else out[field] = input.value.trim() || null;
    });
    return out;
  });
}

function showReview(rules) {
  current = rules;
  document.getElementById('review').hidden = false;
  document.getElementById('summary').textContent =
    rules.summary || 'No summary was returned.';

  const state = document.getElementById('state');
  state.textContent = rules.confirmed ? 'confirmed' : 'draft';
  state.className = `pill ${rules.confirmed ? 'pill-success' : 'pill-warning'}`;

  const unclear = rules.unclear || [];
  document.getElementById('unclear-box').hidden = unclear.length === 0;
  document.getElementById('unclear').textContent = unclear.join('; ');

  renderGroups(rules.groups || []);

  const locked = !canEdit || rules.confirmed;
  document.getElementById('save').hidden = locked;
  document.getElementById('confirm').hidden = locked;
  document.getElementById('discard').hidden = !canEdit;

  document.getElementById('review').scrollIntoView({ behavior: 'smooth',
                                                    block: 'nearest' });
}

/* --- History --------------------------------------------------------------- */

async function loadHistory() {
  const rows = await apiCall('GET', '/intake/requirements');
  document.getElementById('history-empty').hidden = rows.length > 0;

  document.getElementById('history').innerHTML = rows.map((r) => `
    <tr>
      <td>${escapeHtml(r.name)}</td>
      <td><span class="pill ${r.confirmed ? 'pill-success' : ''}">
        ${r.confirmed ? 'confirmed' : 'draft'}</span></td>
      <td>${(r.groups || []).length}</td>
      <td class="muted">${formatLocalTime(r.created_at)}</td>
      <td style="text-align:right">
        <button class="btn small" data-open="${r.id}" type="button">Open</button>
      </td>
    </tr>`).join('');

  document.querySelectorAll('[data-open]').forEach((button) => {
    button.addEventListener('click', async () => {
      try {
        showReview(await apiCall('GET', `/intake/requirements/${button.dataset.open}`));
      } catch (err) {
        showAlert(alertBox, err.message);
      }
    });
  });
}

/* --- Actions --------------------------------------------------------------- */

document.getElementById('submit').addEventListener('click', async () => {
  hideAlert(alertBox);
  const name = document.getElementById('name').value.trim();
  const raw = document.getElementById('raw').value.trim();

  if (!name) return showAlert(alertBox, 'Give this rule set a name.');
  if (raw.length < 10) {
    return showAlert(alertBox, 'Describe the rules in a sentence or two first.');
  }

  const button = document.getElementById('submit');
  button.disabled = true;
  button.textContent = 'Reading it...';
  // Interpretation is an AI call and takes a few seconds; say so rather than
  // leaving a dead button.
  document.getElementById('submit-note').textContent =
    'Interpreting - this takes a moment.';
  try {
    const created = await apiCall('POST', '/intake/requirements',
                                  { name, raw_input: raw });
    showReview(await apiCall('GET', `/intake/requirements/${created.id}`));
    await loadHistory();
    document.getElementById('raw').value = '';
    document.getElementById('name').value = '';
  } catch (err) {
    showAlert(alertBox, err.message);
  } finally {
    button.disabled = false;
    button.textContent = 'Interpret this';
    document.getElementById('submit-note').textContent = '';
  }
});

document.getElementById('save').addEventListener('click', async () => {
  hideAlert(alertBox);
  try {
    await apiCall('PUT', `/intake/requirements/${current.id}`,
                  { groups: readGroups() });
    showReview(await apiCall('GET', `/intake/requirements/${current.id}`));
    showAlert(alertBox, 'Corrections saved.', 'success');
    await loadHistory();
  } catch (err) {
    showAlert(alertBox, err.message);
  }
});

document.getElementById('confirm').addEventListener('click', async () => {
  hideAlert(alertBox);
  const ok = window.confirm(
    'Confirm these rules? They will shape the next generation, and will '
    + 'replace any rule set you confirmed earlier.');
  if (!ok) return;
  try {
    // Corrections on screen are saved first, so confirming never commits a
    // version the school did not see.
    await apiCall('PUT', `/intake/requirements/${current.id}`,
                  { groups: readGroups() });
    await apiCall('POST', `/intake/requirements/${current.id}/confirm`);
    showReview(await apiCall('GET', `/intake/requirements/${current.id}`));
    showAlert(alertBox, 'Confirmed. The next generation will use these.',
              'success');
    await loadHistory();
  } catch (err) {
    showAlert(alertBox, err.message);
  }
});

document.getElementById('discard').addEventListener('click', async () => {
  if (!window.confirm('Delete this rule set?')) return;
  try {
    await apiCall('DELETE', `/intake/requirements/${current.id}`);
    document.getElementById('review').hidden = true;
    current = null;
    await loadHistory();
  } catch (err) {
    showAlert(alertBox, err.message);
  }
});

/* --- Load ------------------------------------------------------------------ */

(async () => {
  const me = await requireLogin();
  if (!me) return;

  document.getElementById('who').textContent =
    `${me.first_name} ${me.surname} - ${me.role}`;
  canEdit = me.role === 'owner' || me.role === 'dev';
  if (!canEdit) {
    document.getElementById('submit').disabled = true;
    document.getElementById('submit-note').textContent =
      'Only the owner can change class rules.';
  }

  await loadHistory();
  document.getElementById('loading').hidden = true;
  document.getElementById('content').hidden = false;
})().catch((err) => {
  document.getElementById('loading').hidden = true;
  showAlert(alertBox, err.message);
});
