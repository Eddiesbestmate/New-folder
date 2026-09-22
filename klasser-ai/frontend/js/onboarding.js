/* Klasser - onboarding wizard.
 *
 * Progress is always read from the backend, which derives each step from the
 * actual data. Nothing here decides whether a step is complete.
 */

const alertBox = document.getElementById('alert');
const progressList = document.getElementById('progress');
const upcomingList = document.getElementById('upcoming');
const campusRows = document.getElementById('campus-rows');

const LATER_STEPS = ['step_layout', 'step_subjects', 'step_transport'];

document.getElementById('logout').addEventListener('click', logout);

/* --- Campus rows ---------------------------------------------------------- */

function addCampusRow(campus = { name: '', address: '' }) {
  const row = document.createElement('div');
  row.className = 'field-row campus-row';
  row.style.marginBottom = '8px';
  row.innerHTML = `
    <input class="campus-name" placeholder="Campus name" maxlength="120">
    <input class="campus-address" placeholder="Address (optional)" maxlength="300">
  `;

  const remove = document.createElement('button');
  remove.type = 'button';
  remove.className = 'btn';
  remove.textContent = 'Remove';
  remove.style.flex = '0 0 auto';
  remove.addEventListener('click', () => {
    if (campusRows.querySelectorAll('.campus-row').length > 1) row.remove();
    else showAlert(alertBox, 'A school needs at least one campus.', 'warning');
  });

  row.appendChild(remove);
  row.querySelector('.campus-name').value = campus.name || '';
  row.querySelector('.campus-address').value = campus.address || '';
  campusRows.appendChild(row);
}

document.getElementById('add-campus')
  .addEventListener('click', () => addCampusRow());

function readCampuses() {
  return Array.from(campusRows.querySelectorAll('.campus-row'))
    .map((row) => ({
      name: row.querySelector('.campus-name').value.trim(),
      address: row.querySelector('.campus-address').value.trim() || null,
    }))
    .filter((c) => c.name);
}

/* --- Rendering ------------------------------------------------------------ */

function renderProgress(progress) {
  const render = (steps) => steps.map((s) => {
    const count = s.count !== null && s.count !== undefined ? ` (${s.count})` : '';
    const note = s.note && !s.complete ? ` <span class="muted small">- ${s.note}</span>` : '';
    const req = s.required ? '' : ' <span class="muted small">optional</span>';
    return `
      <li>
        <span class="tick ${s.complete ? 'done' : 'pending'}">${s.complete ? '&check;' : '&#9675;'}</span>
        <span>${s.label}${count}${req}${note}</span>
      </li>`;
  }).join('');

  progressList.innerHTML = render(
    progress.steps.filter((s) => !LATER_STEPS.includes(s.key))
  );
  upcomingList.innerHTML = render(
    progress.steps.filter((s) => LATER_STEPS.includes(s.key))
  );

  const ready = document.getElementById('ready');
  if (progress.ready_to_generate) {
    ready.className = 'alert alert-success small';
    ready.textContent = 'Ready to generate a timetable.';
  } else {
    ready.className = 'alert alert-info small';
    ready.textContent = `Still needed: ${progress.missing.join(', ')}.`;
  }
}

async function refreshProgress() {
  renderProgress(await apiCall('GET', '/onboarding/progress'));
}

function renderPackages(billing) {
  document.getElementById('billing_mode').value = billing.billing_mode;

  document.getElementById('packages').innerHTML = billing.packages
    .map((p) => {
      const unavailable = p.available
        ? ''
        : '<div class="small" style="color:var(--text-warning)">Already used</div>';
      const expiry = p.expires_days
        ? `<div class="small muted">Expires after ${p.expires_days} days</div>`
        : '';
      return `
        <div class="card" style="padding:14px">
          <h3>${p.display_name}</h3>
          <div style="font-size:20px;font-weight:700">
            $${p.price_dollars.toLocaleString()}
          </div>
          <div class="small muted">
            ${p.credits.toLocaleString()} credits &middot; $${p.price_per_credit.toFixed(2)}/credit
          </div>
          ${expiry}${unavailable}
        </div>`;
    })
    .join('') +
    `<div class="card" style="padding:14px">
       <h3>Pay as you go</h3>
       <div style="font-size:20px;font-weight:700">
         $${billing.payg_rate_dollars.toFixed(2)}
       </div>
       <div class="small muted">per credit, charged after each generation</div>
     </div>`;
}

/* --- Save handlers -------------------------------------------------------- */

document.getElementById('details-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  hideAlert(alertBox);

  const campuses = readCampuses();
  if (!campuses.length) {
    showAlert(alertBox, 'Add at least one campus.');
    return;
  }

  const names = campuses.map((c) => c.name.toLowerCase());
  if (new Set(names).size !== names.length) {
    showAlert(alertBox, 'Campus names must be unique.');
    return;
  }

  const btn = document.getElementById('save-details');
  btn.disabled = true;
  btn.textContent = 'Saving...';
  try {
    const res = await apiCall('PUT', '/onboarding/school-details', {
      school_name: document.getElementById('school_name').value.trim(),
      timezone: document.getElementById('timezone').value,
      campuses,
    });
    await refreshProgress();
    showAlert(alertBox, res.message || 'Saved.', 'success');
  } catch (err) {
    showAlert(alertBox, err.message);
  } finally {
    btn.disabled = false;
    btn.textContent = 'Save school details';
  }
});

document.getElementById('save-billing').addEventListener('click', async () => {
  hideAlert(alertBox);
  const btn = document.getElementById('save-billing');
  btn.disabled = true;
  btn.textContent = 'Saving...';
  try {
    await apiCall('PUT', '/onboarding/billing-mode', {
      billing_mode: document.getElementById('billing_mode').value,
    });
    await refreshProgress();
    showAlert(alertBox, 'Billing choice saved.', 'success');
  } catch (err) {
    showAlert(alertBox, err.message);
  } finally {
    btn.disabled = false;
    btn.textContent = 'Save billing choice';
  }
});

/* --- Load ----------------------------------------------------------------- */

(async () => {
  const me = await requireLogin();
  if (!me) return;

  document.getElementById('who').textContent =
    `${me.first_name} ${me.surname} - ${me.role}`;

  const [zones, details, billing, progress] = await Promise.all([
    apiCall('GET', '/auth/timezones'),
    apiCall('GET', '/onboarding/school-details'),
    apiCall('GET', '/onboarding/billing-options'),
    apiCall('GET', '/onboarding/progress'),
  ]);

  document.getElementById('timezone').innerHTML = zones
    .map((z) => `<option value="${z.value}">${z.label} (${z.value})</option>`)
    .join('');

  document.getElementById('school_name').value = details.school_name;
  document.getElementById('timezone').value = details.timezone;

  if (details.campuses.length) details.campuses.forEach(addCampusRow);
  else addCampusRow();

  renderPackages(billing);
  renderProgress(progress);

  document.getElementById('loading').hidden = true;
  document.getElementById('content').hidden = false;
})().catch((err) => {
  document.getElementById('loading').hidden = true;
  showAlert(alertBox, err.message);
});
