/* Klasser AI - live generation progress.
 *
 * Streams events over SSE, falling back to polling if the stream cannot be
 * opened. Either way the page reconstructs state from the server on load, so
 * closing the tab and coming back mid-generation shows the real position
 * rather than starting from zero.
 */

const alertBox = document.getElementById('alert');
const params = new URLSearchParams(window.location.search);
const timetableId = params.get('timetable');

let attemptId = null;
let stream = null;
let poller = null;
let finished = false;

document.getElementById('logout').addEventListener('click', logout);

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));
}

/* --- Rendering ------------------------------------------------------------ */

function renderStages(status) {
  const done = {};
  for (const s of status.stages || []) done[s.stage] = s.status;

  document.getElementById('stages').innerHTML = (status.all_stages || [])
    .map((s) => {
      const state = done[s.key];
      let cls = 'waiting';
      let mark = '&#9675;';
      if (state === 'complete') { cls = 'done'; mark = '&check;'; }
      else if (state === 'failed') { cls = 'failed'; mark = '&times;'; }
      else if (state === 'in_progress' || s.key === status.current_stage) {
        cls = 'active'; mark = '&#9679;';
      }
      return `<li><span class="dot ${cls}">${mark}</span>
                  <span>${escapeHtml(s.label)}</span></li>`;
    }).join('');
}

function renderProgress(status) {
  const pct = Math.max(0, Math.min(100, status.progress_pct || 0));
  document.getElementById('bar-fill').style.width = `${pct}%`;
  document.getElementById('pct').textContent = `${pct}%`;

  const label = (status.all_stages || [])
    .find((s) => s.key === status.current_stage);
  document.getElementById('stage-label').textContent =
    label ? label.label : (status.job_status || 'Working...');
  document.getElementById('chunk').textContent = status.current_chunk || '';

  renderStages(status);
}

const seen = new Set();

function appendLog(event) {
  const signature = `${event.type}:${event.message}`;
  if (seen.has(signature)) return;
  seen.add(signature);

  const cls = ['retry', 'failed', 'complete'].includes(event.type) ? event.type : '';
  const box = document.getElementById('log');

  // The placeholder goes the moment there is anything real to show. Without
  // it, someone who has just started a run watches an empty box for the first
  // half-minute and assumes nothing is happening.
  const placeholder = document.getElementById('log-empty');
  if (placeholder) placeholder.remove();

  const line = document.createElement('div');
  if (cls) line.className = cls;
  line.textContent = event.message;
  box.appendChild(line);
  box.scrollTop = box.scrollHeight;
}

function finish(status) {
  if (finished) return;
  finished = true;
  if (stream) stream.close();
  if (poller) clearInterval(poller);

  const box = document.getElementById('result');
  box.hidden = false;

  if (status.status === 'complete' || status.job_status === 'complete') {
    box.className = 'alert alert-success';
    box.textContent = 'Timetable generated.';
    if (status.version_id) {
      const link = document.getElementById('view-output');
      link.href = `output.html?version=${status.version_id}`;
      link.hidden = false;
    }
  } else {
    box.className = 'alert alert-error';
    box.textContent = status.failure_reason
      || 'Generation failed. See the activity log above.';
    document.getElementById('retry').hidden = false;
  }
}

/* --- Polling fallback ------------------------------------------------------ */

async function pollOnce() {
  try {
    const status = await apiCall('GET', `/allocation/${attemptId}/status`);
    renderProgress(status);

    const events = await apiCall('GET', `/allocation/${attemptId}/events`);
    events.forEach((e) => appendLog({ type: e.event_type, message: e.message }));

    if (['complete', 'failed'].includes(status.status)
        || ['complete', 'failed'].includes(status.job_status)) {
      finish(status);
    }
  } catch (err) {
    // A transient failure should not kill the page; the next tick retries.
    console.warn('poll failed', err);
  }
}

function startPolling() {
  if (poller) return;
  poller = setInterval(pollOnce, 2000);
  pollOnce();
}

/* --- SSE ------------------------------------------------------------------- */

async function startStream() {
  const token = await accessToken();
  if (!token) return startPolling();

  // EventSource cannot set headers, so the token goes in the query string and
  // the endpoint verifies it before streaming anything.
  stream = new EventSource(
    `${KLASSER.API_BASE}/allocation/${attemptId}/progress?token=${encodeURIComponent(token)}`
  );

  stream.onmessage = (message) => {
    let data;
    try {
      data = JSON.parse(message.data);
    } catch {
      return;
    }

    // The server closes a stream that has run too long without its job
    // finishing. Say so and fall back to polling, rather than sitting on a
    // silent connection that will never speak again.
    if (data.type === 'stream_timeout') {
      appendLog({ type: 'retry', message: data.message });
      stream.close();
      startPolling();
      return;
    }

    if (data.type === 'progress') {
      renderProgress({
        progress_pct: data.progress_pct,
        current_stage: data.current_stage,
        current_chunk: data.current_chunk,
        job_status: data.status,
        all_stages: allStages,
        stages: lastStages,
      });
      if (['complete', 'failed'].includes(data.status)) {
        // Re-read the authoritative status for the version id and reason.
        apiCall('GET', `/allocation/${attemptId}/status`).then(finish);
      }
      return;
    }
    appendLog(data);
  };

  stream.onerror = () => {
    // Falling back rather than leaving the page frozen on a dead stream.
    stream.close();
    stream = null;
    startPolling();
  };
}

/* --- Start ----------------------------------------------------------------- */

let allStages = [];
let lastStages = [];

(async () => {
  const me = await requireLogin();
  if (!me) return;

  document.getElementById('who').textContent =
    `${me.first_name} ${me.surname} - ${me.role}`;

  if (!timetableId) {
    showAlert(alertBox, 'No timetable specified.');
    document.getElementById('loading').hidden = true;
    return;
  }

  // The retry loop creates an attempt per try, so take the most recent.
  const attempts = await apiCall(
    'GET', `/allocation/timetable/${timetableId}/attempts`);
  if (!attempts.length) {
    showAlert(alertBox, 'This generation has not started yet. Refresh in a moment.');
    document.getElementById('loading').hidden = true;
    return;
  }
  attemptId = attempts[attempts.length - 1].id;

  const status = await apiCall('GET', `/allocation/${attemptId}/status`);
  allStages = status.all_stages || [];
  lastStages = status.stages || [];

  document.getElementById('title').textContent = status.name;
  document.getElementById('subtitle').textContent =
    attempts.length > 1
      ? `Attempt ${attempts.length} - earlier attempts are kept in the history`
      : 'This runs in the background. You can safely close this page.';

  renderProgress(status);

  const events = await apiCall('GET', `/allocation/${attemptId}/events`);
  events.forEach((e) => appendLog({ type: e.event_type, message: e.message }));

  document.getElementById('loading').hidden = true;
  document.getElementById('content').hidden = false;

  if (['complete', 'failed'].includes(status.status)
      || ['complete', 'failed'].includes(status.job_status)) {
    finish(status);
  } else {
    startStream();
  }
})().catch((err) => {
  document.getElementById('loading').hidden = true;
  showAlert(alertBox, err.message);
});
