/* Klasser - timetable layout builder.
 *
 * Edits an in-memory model of days and periods, then sends the whole layout in
 * one PUT. The backend replaces the period set atomically, so a partial save
 * cannot leave a half-built cycle.
 */

const alertBox = document.getElementById('alert');
const container = document.getElementById('days-container');

const PERIOD_TYPES = ['teaching', 'mentor', 'break', 'lunch', 'assembly', 'blocked'];

const DEFAULT_DAY = [
  { period_type: 'teaching', start_time: '09:00', end_time: '10:00', label: 'Period 1' },
  { period_type: 'teaching', start_time: '10:00', end_time: '11:00', label: 'Period 2' },
  { period_type: 'break',    start_time: '11:00', end_time: '11:20', label: 'Recess' },
  { period_type: 'teaching', start_time: '11:20', end_time: '12:20', label: 'Period 3' },
  { period_type: 'teaching', start_time: '12:20', end_time: '13:20', label: 'Period 4' },
  { period_type: 'lunch',    start_time: '13:20', end_time: '14:00', label: 'Lunch' },
  { period_type: 'teaching', start_time: '14:00', end_time: '15:00', label: 'Period 5' },
];

let model = []; // model[dayIndex] = array of periods

document.getElementById('logout').addEventListener('click', logout);

/* Period labels are typed by the school and rendered straight back into an
   HTML attribute. Unescaped, a label of `" onfocus="...` escapes the attribute
   and runs for everyone at that school who opens this page. Quotes matter as
   much as angle brackets here, because the value sits inside value="...". */
function escapeHtml(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));
}

/* --- Rendering ------------------------------------------------------------- */

function render() {
  container.innerHTML = model.map((periods, d) => `
    <div class="day-block">
      <div style="display:flex;align-items:center;gap:10px;margin-bottom:10px">
        <h3 style="margin:0">Day ${d + 1}</h3>
        <span class="muted small">${periods.length} periods</span>
        <button class="btn small" data-add="${d}" style="margin-left:auto">+ Period</button>
      </div>
      <div class="period-row muted small" style="font-weight:600">
        <span>#</span><span>Type</span><span>Start</span><span>End</span>
        <span>Label</span><span></span>
      </div>
      ${periods.map((p, i) => `
        <div class="period-row">
          <span class="muted small">${i + 1}</span>
          <select data-d="${d}" data-i="${i}" data-k="period_type">
            ${PERIOD_TYPES.map((t) =>
              `<option value="${t}" ${p.period_type === t ? 'selected' : ''}>${t}</option>`
            ).join('')}
          </select>
          <input type="time" value="${escapeHtml(p.start_time)}" data-d="${d}" data-i="${i}" data-k="start_time">
          <input type="time" value="${escapeHtml(p.end_time)}"   data-d="${d}" data-i="${i}" data-k="end_time">
          <input type="text" value="${escapeHtml(p.label || '')}" placeholder="Label"
                 data-d="${d}" data-i="${i}" data-k="label">
          <button class="btn small" data-del="${d}:${i}">Remove</button>
        </div>`).join('')}
    </div>
  `).join('');

  container.querySelectorAll('[data-k]').forEach((el) => {
    el.addEventListener('change', () => {
      model[Number(el.dataset.d)][Number(el.dataset.i)][el.dataset.k] = el.value;
      updateSummary();
    });
  });

  container.querySelectorAll('[data-add]').forEach((btn) => {
    btn.addEventListener('click', () => {
      const d = Number(btn.dataset.add);
      const last = model[d][model[d].length - 1];
      model[d].push({
        period_type: 'teaching',
        start_time: last ? last.end_time : '09:00',
        end_time: last ? addMinutes(last.end_time, 60) : '10:00',
        label: `Period ${model[d].length + 1}`,
      });
      render();
    });
  });

  container.querySelectorAll('[data-del]').forEach((btn) => {
    btn.addEventListener('click', () => {
      const [d, i] = btn.dataset.del.split(':').map(Number);
      model[d].splice(i, 1);
      render();
    });
  });

  updateSummary();
}

function addMinutes(hhmm, mins) {
  const [h, m] = hhmm.split(':').map(Number);
  const total = h * 60 + m + mins;
  const hh = String(Math.floor(total / 60) % 24).padStart(2, '0');
  const mm = String(total % 60).padStart(2, '0');
  return `${hh}:${mm}`;
}

function updateSummary() {
  const total = model.reduce((n, d) => n + d.length, 0);
  const teaching = model.reduce(
    (n, d) => n + d.filter((p) => p.period_type === 'teaching').length, 0);
  document.getElementById('summary').textContent =
    `${total} slots across ${model.length} days - ${teaching} teaching`;
}

/* --- Controls -------------------------------------------------------------- */

document.getElementById('apply-days').addEventListener('click', () => {
  const want = Math.max(1, Math.min(20, Number(document.getElementById('days').value) || 1));
  while (model.length < want) model.push(DEFAULT_DAY.map((p) => ({ ...p })));
  if (model.length > want) {
    if (!confirm(`Remove day ${want + 1} onwards?`)) return;
    model.length = want;
  }
  render();
});

document.getElementById('copy-day1').addEventListener('click', () => {
  if (!model.length) return;
  const first = model[0];
  for (let d = 1; d < model.length; d += 1) {
    model[d] = first.map((p) => ({ ...p }));
  }
  render();
  showAlert(alertBox, 'Day 1 copied to every other day.', 'success');
});

/* --- Save ------------------------------------------------------------------ */

document.getElementById('save').addEventListener('click', async () => {
  hideAlert(alertBox);

  const periods = [];
  for (let d = 0; d < model.length; d += 1) {
    if (!model[d].length) {
      showAlert(alertBox, `Day ${d + 1} has no periods. Add at least one or reduce the cycle length.`);
      return;
    }
    model[d].forEach((p, i) => {
      periods.push({
        day_number: d + 1,
        period_number: i + 1,
        period_type: p.period_type,
        start_time: p.start_time,
        end_time: p.end_time,
        label: p.label || null,
      });
    });
  }

  /* Catch the obvious errors here so the user is not round-tripping to the
   * server for something visible on screen. The backend checks again. */
  for (const p of periods) {
    if (!p.start_time || !p.end_time) {
      showAlert(alertBox, `Day ${p.day_number} period ${p.period_number} is missing a time.`);
      return;
    }
    if (p.start_time >= p.end_time) {
      showAlert(alertBox,
        `Day ${p.day_number} period ${p.period_number}: start must be before end.`);
      return;
    }
  }

  const btn = document.getElementById('save');
  btn.disabled = true;
  btn.textContent = 'Saving...';
  try {
    const res = await apiCall('PUT', '/schools/layout', {
      name: document.getElementById('name').value.trim() || 'Standard cycle',
      days_in_cycle: model.length,
      periods,
    });
    showAlert(alertBox, res.message || 'Layout saved.', 'success');
  } catch (err) {
    showAlert(alertBox, err.message);
  } finally {
    btn.disabled = false;
    btn.textContent = 'Save layout';
  }
});

/* --- Start ----------------------------------------------------------------- */

(async () => {
  const me = await requireLogin();
  if (!me) return;

  document.getElementById('who').textContent =
    `${me.first_name} ${me.surname} - ${me.role}`;

  const existing = await apiCall('GET', '/schools/layout');

  if (existing) {
    document.getElementById('name').value = existing.name;
    document.getElementById('days').value = existing.days_in_cycle;

    model = Array.from({ length: existing.days_in_cycle }, () => []);
    existing.periods.forEach((p) => {
      model[p.day_number - 1].push({
        period_type: p.period_type,
        start_time: p.start_time,
        end_time: p.end_time,
        label: p.label,
      });
    });
  } else {
    document.getElementById('name').value = 'Standard 5-day cycle';
    model = Array.from({ length: 5 }, () => DEFAULT_DAY.map((p) => ({ ...p })));
  }

  render();
  document.getElementById('loading').hidden = true;
  document.getElementById('content').hidden = false;
})().catch((err) => {
  document.getElementById('loading').hidden = true;
  showAlert(alertBox, err.message);
});
