/* Klasser - notification preferences. Auto-saves; no save button. */

const alertBox = document.getElementById('alert');

let pending = {};
let timer = null;

document.getElementById('logout').addEventListener('click', logout);

function escapeHtml(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));
}

function render(groups) {
  document.getElementById('groups').innerHTML = groups.map((g) => `
    <div class="card" style="margin-bottom:14px">
      <h3 style="margin-top:0">${escapeHtml(g.group)}</h3>
      ${g.items.map((item) => `
        <label class="pref">
          <input type="checkbox" data-key="${escapeHtml(item.key)}"
                 ${item.enabled ? 'checked' : ''}>
          <span>
            <span class="label">${escapeHtml(item.label)}</span><br>
            <span class="detail">${escapeHtml(item.detail)}</span>
          </span>
        </label>`).join('')}
    </div>`).join('');

  document.querySelectorAll('[data-key]').forEach((box) => {
    box.addEventListener('change', () => queue(box.dataset.key, box.checked));
  });
}

/* Debounced: flicking six toggles sends one request, not six. */
function queue(key, value) {
  pending[key] = value;
  clearTimeout(timer);
  timer = setTimeout(flush, 600);
}

async function flush() {
  if (!Object.keys(pending).length) return;
  const batch = pending;
  pending = {};

  try {
    await apiCall('PUT', '/notifications', { preferences: batch });
    const saved = document.getElementById('saved');
    saved.classList.add('show');
    setTimeout(() => saved.classList.remove('show'), 1400);
  } catch (err) {
    showAlert(alertBox, `Could not save: ${err.message}`);
    // Put the toggles back where the server still has them, rather than
    // leaving the page showing a change that did not stick.
    await load();
  }
}

document.getElementById('all-on').addEventListener('click', () => setAll(true));
document.getElementById('all-off').addEventListener('click', () => setAll(false));

function setAll(value) {
  document.querySelectorAll('[data-key]').forEach((box) => {
    box.checked = value;
    pending[box.dataset.key] = value;
  });
  clearTimeout(timer);
  flush();
}

async function load() {
  const data = await apiCall('GET', '/notifications');
  document.getElementById('intro').textContent =
    `Sent to ${data.email}. Changes save as you make them.`;
  render(data.groups);
}

(async () => {
  const me = await requireLogin();
  if (!me) return;

  document.getElementById('who').textContent =
    `${me.first_name} ${me.surname} - ${me.role}`;

  await load();

  document.getElementById('loading').hidden = true;
  document.getElementById('content').hidden = false;
})().catch((err) => {
  document.getElementById('loading').hidden = true;
  showAlert(alertBox, err.message);
});
