/* Klasser - request a timetable change in plain English. */

const alertBox = document.getElementById('alert');
const params = new URLSearchParams(window.location.search);
const versionId = params.get('version');

let current = null;

document.getElementById('logout').addEventListener('click', logout);

function escapeHtml(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));
}

const EXAMPLES = [
  'Move 11COM1 out of Period 3 on Day 4',
  "Mr Smith shouldn't teach on Fridays",
  'Swap the rooms for 11PHY1 and 11BIO1 on Day 6',
  'Put 10ENG2 in a bigger room',
];

document.getElementById('examples').innerHTML = EXAMPLES
  .map((e) => `<button type="button" data-example="${escapeHtml(e)}">${escapeHtml(e)}</button>`)
  .join('');

document.querySelectorAll('[data-example]').forEach((btn) => {
  btn.addEventListener('click', () => {
    document.getElementById('request').value = btn.dataset.example;
    document.getElementById('request').focus();
  });
});

/* --- The proposal ---------------------------------------------------------- */

const VERDICTS = {
  pass: ['alert-success', 'This change is valid and can be applied.'],
  fail: ['alert-danger', 'This change cannot be applied.'],
  clarify: ['alert-warning', 'This request needs to be more specific.'],
};

function side(s, cls) {
  if (!s) return '<div class="side muted">-</div>';
  return `
    <div class="side ${cls}">
      <div class="head">Day ${s.day} &middot; ${escapeHtml(s.period)}</div>
      <div class="muted">${escapeHtml(s.teacher)}</div>
      <div class="muted">${escapeHtml(s.room)}</div>
    </div>`;
}

function renderProposal(edit) {
  current = edit;
  document.getElementById('proposal').hidden = false;
  document.getElementById('interpretation').textContent = edit.interpretation;

  const [cls, blurb] = VERDICTS[edit.validator_result] || VERDICTS.fail;
  const box = document.getElementById('verdict');
  box.className = `verdict ${cls}`;
  box.innerHTML = `<strong>${blurb}</strong>`
    + (edit.validator_detail ? `<br>${escapeHtml(edit.validator_detail)}` : '');

  document.getElementById('changes').innerHTML = edit.changes.map((c) => `
    <div class="change">
      ${side(c.before, 'before')}
      <div class="arrow">&rarr;</div>
      ${side(c.after, 'after')}
    </div>
    <p class="small muted" style="margin:4px 0 0">
      ${escapeHtml(c.class_code)}${c.description ? ` - ${escapeHtml(c.description)}` : ''}
    </p>`).join('');

  // A proposal that failed validation can still be discarded, so the row stays
  // visible - only the apply button is conditional.
  document.getElementById('apply').hidden = !edit.can_apply;
  document.getElementById('proposal').scrollIntoView({ behavior: 'smooth' });
}

document.getElementById('ask').addEventListener('click', async () => {
  const text = document.getElementById('request').value.trim();
  if (text.length < 3) {
    showAlert(alertBox, 'Describe the change you want in a sentence.');
    return;
  }

  const button = document.getElementById('ask');
  button.disabled = true;
  document.getElementById('thinking').hidden = false;
  document.getElementById('proposal').hidden = true;

  try {
    const edit = await apiCall('POST', '/editing/request',
      { version_id: versionId, request_text: text });
    renderProposal(edit);
    await loadHistory();
  } catch (err) {
    showAlert(alertBox, err.message);
  } finally {
    button.disabled = false;
    document.getElementById('thinking').hidden = true;
  }
});

document.getElementById('apply').addEventListener('click', async () => {
  if (!current) return;
  const button = document.getElementById('apply');
  button.disabled = true;
  try {
    const result = await apiCall('POST', `/editing/${current.id}/apply`);
    showAlert(alertBox, result.already_applied
      ? 'That change was already applied.'
      : `Change applied. ${result.credits_charged} credits used.`, 'success');
    document.getElementById('proposal').hidden = true;
    document.getElementById('request').value = '';
    current = null;
    await loadHistory();
  } catch (err) {
    showAlert(alertBox, err.message);
  } finally {
    button.disabled = false;
  }
});

document.getElementById('reject').addEventListener('click', async () => {
  if (!current) return;
  try {
    await apiCall('POST', `/editing/${current.id}/reject`);
    document.getElementById('proposal').hidden = true;
    current = null;
    await loadHistory();
  } catch (err) {
    showAlert(alertBox, err.message);
  }
});

/* --- History --------------------------------------------------------------- */

async function loadHistory() {
  const rows = await apiCall('GET', `/editing/version/${versionId}/history`);
  const box = document.getElementById('history');

  if (!rows.length) {
    box.innerHTML = '<p class="small muted">No requests yet.</p>';
    return;
  }

  box.innerHTML = `
    <table class="table">
      <thead><tr>
        <th>Request</th><th>Outcome</th><th>Credits</th><th>By</th>
      </tr></thead>
      <tbody>${rows.map((e) => `
        <tr>
          <td>
            ${escapeHtml(e.request_text)}
            <br><span class="small muted">${escapeHtml(e.ai_interpretation || '')}</span>
          </td>
          <td>
            ${escapeHtml(e.status)}
            ${e.validator_detail
              ? `<br><span class="small muted">${escapeHtml(e.validator_detail)}</span>`
              : ''}
          </td>
          <td>${e.status === 'applied' ? e.credits_charged : '-'}</td>
          <td class="small muted">${escapeHtml(e.requested_by)}</td>
        </tr>`).join('')}
      </tbody>
    </table>`;
}

/* --- Load ------------------------------------------------------------------ */

(async () => {
  const me = await requireLogin();
  if (!me) return;

  document.getElementById('who').textContent =
    `${me.first_name} ${me.surname} - ${me.role}`;

  if (!versionId) {
    document.getElementById('loading').hidden = true;
    showAlert(alertBox, 'No version selected. Open one from Timetables.');
    return;
  }

  const summary = await apiCall('GET', `/timetables/version/${versionId}/summary`);
  document.getElementById('subtitle').textContent =
    `${summary.name} - v${summary.version_number} (${summary.status})`;
  document.getElementById('back').href =
    `timetables.html?timetable=${summary.timetable_id}`;

  // Only a draft is editable: a published timetable is what the school is
  // running on, so changes to it go through a new version.
  if (summary.status !== 'draft') {
    showAlert(alertBox,
      `This version is ${summary.status}. Only a draft can be edited - generate `
      + 'a new version, or roll back to a draft.');
    document.getElementById('ask').disabled = true;
    document.getElementById('request').disabled = true;
  }

  if (me.role !== 'owner') {
    document.getElementById('ask').disabled = true;
    document.getElementById('request').disabled = true;
    showAlert(alertBox, 'Only the account owner can request changes.');
  }

  const account = await apiCall('GET', '/billing/account');
  document.getElementById('cost').textContent = account.edit_session_cost ?? 10;

  await loadHistory();

  document.getElementById('loading').hidden = true;
  document.getElementById('content').hidden = false;
})().catch((err) => {
  document.getElementById('loading').hidden = true;
  showAlert(alertBox, err.message);
});
