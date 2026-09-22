/* Klasser - live generation progress.
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
let canStop = false;

// Terminal states. 'cancelled' belongs here as much as the other two: without
// it, stopping a run leaves the page polling a job that will never change.
const DONE = ['complete', 'failed', 'cancelled'];
const isDone = (status) => DONE.includes(status.status)
  || DONE.includes(status.job_status);

document.getElementById('logout').addEventListener('click', logout);

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));
}

/* --- Rendering ------------------------------------------------------------ */

/* Reconstructs a {stage, status} list from a single current_stage marker,
 * for the SSE path where the server does not send full stage history on
 * every tick. Stages always run in all_stages order, so anything before the
 * current one is done, the current one is active (or failed, on a dead job),
 * and the rest have not started. */
function stagesFromPosition(allStagesList, currentStage, jobStatus) {
  const index = allStagesList.findIndex((s) => s.key === currentStage);
  return allStagesList.map((s, i) => {
    let stageStatus = 'in_progress';
    if (index === -1) stageStatus = 'in_progress';
    else if (i < index) stageStatus = 'complete';
    else if (i === index) stageStatus = jobStatus === 'failed' ? 'failed' : 'in_progress';
    else stageStatus = 'pending';
    return { stage: s.key, status: stageStatus };
  });
}

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

  setWorking(!finished);
  renderStages(status);
}

/* A stage can hold the same percentage for minutes while the AI works a chunk,
 * so the only honest signal that anything is happening is motion that does not
 * depend on the number changing. */
function setWorking(live) {
  document.getElementById('bar-fill').parentElement
    .classList.toggle('working', live);
  document.getElementById('content').classList.toggle('working-now', live);
  const stop = document.getElementById('stop');
  if (stop) stop.hidden = !live || !canStop;
}

let elapsedTimer = null;
let startedAt = null;

function startElapsed(fromIso) {
  startedAt = fromIso ? new Date(fromIso).getTime() : Date.now();
  const tick = () => {
    // Self-terminating: a background tab can throttle setInterval down to
    // once a minute or less rather than actually stopping it, so refocusing
    // the window after the run had already finished could show the count
    // jump forward instead of sitting still - clearInterval had fired, but
    // a throttled callback already in flight could still land after it. This
    // makes the tick itself the authority on whether it should still exist.
    if (finished) {
      clearInterval(elapsedTimer);
      elapsedTimer = null;
      return;
    }
    const seconds = Math.max(0, Math.floor((Date.now() - startedAt) / 1000));
    const mm = String(Math.floor(seconds / 60)).padStart(2, '0');
    const ss = String(seconds % 60).padStart(2, '0');
    document.getElementById('elapsed').textContent = `${mm}:${ss} · `;
  };
  tick();
  elapsedTimer = setInterval(tick, 1000);
  // Force an immediate, accurate repaint on refocus rather than waiting for
  // the next throttled tick - the number is always correct wall-clock time
  // regardless, this just stops it looking frozen right after you tab back.
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden) tick();
  });
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
  if (elapsedTimer) { clearInterval(elapsedTimer); elapsedTimer = null; }
  setWorking(false);

  const box = document.getElementById('result');
  box.hidden = false;

  // The detailed result panel sits below the activity log, which can run to
  // dozens of lines - on a failure, nothing above it said the run had even
  // stopped. showAlert on the top-of-page box scrolls itself into view, so
  // that is now what actually announces the outcome; the panel below still
  // carries the reason and the follow-up buttons.
  if (status.status === 'complete' || status.job_status === 'complete') {
    box.className = 'alert alert-success';
    box.textContent = 'Timetable generated.';
    if (status.version_id) {
      const link = document.getElementById('view-output');
      link.href = `output.html?version=${status.version_id}`;
      link.hidden = false;
    }
    showAlert(alertBox, 'Timetable generated.', 'success');
  } else if (status.status === 'cancelled' || status.job_status === 'cancelled') {
    // Someone chose this. Reporting it in red next to a failure reason would
    // suggest something went wrong.
    box.className = 'alert alert-warning';
    box.textContent = 'Stopped. Nothing was charged for this run.';
    document.getElementById('retry').hidden = false;
    showAlert(alertBox, 'Stopped. Nothing was charged for this run.', 'warning');
  } else {
    const reason = status.failure_reason || 'Generation failed.';
    box.className = 'alert alert-error';
    box.textContent = reason;
    document.getElementById('retry').hidden = false;
    showAlert(alertBox, `Generation halted: ${reason}`, 'error');
  }
}

/* --- Polling fallback ------------------------------------------------------ */

async function pollOnce() {
  try {
    const status = await apiCall('GET', `/allocation/${attemptId}/status`);
    renderProgress(status);

    const events = await apiCall('GET', `/allocation/${attemptId}/events`);
    events.forEach((e) => appendLog({ type: e.event_type, message: e.message }));

    if (isDone(status)) finish(status);
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
      // Stages run in the fixed order the server declares in all_stages, so
      // which ones are done can be read off the position of current_stage -
      // no need for the per-stage history the SSE payload doesn't carry.
      // Using `lastStages` here instead once meant every live update kept
      // re-rendering the tick marks from whatever they were at page load:
      // the bar and label moved, the checklist never did.
      lastStages = stagesFromPosition(allStages, data.current_stage, data.status);
      renderProgress({
        progress_pct: data.progress_pct,
        current_stage: data.current_stage,
        current_chunk: data.current_chunk,
        job_status: data.status,
        all_stages: allStages,
        stages: lastStages,
      });
      if (DONE.includes(data.status)) {
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

/* --- Stopping --------------------------------------------------------------- */

/* The endpoint stops everything this school has in flight, not just the run on
 * screen, so the confirmation says so rather than letting someone assume it is
 * scoped to this page. */
document.getElementById('stop').addEventListener('click', async () => {
  const button = document.getElementById('stop');
  const ok = window.confirm(
    'Stop this generation? Any other generations this school has queued or '
    + 'running will stop too. Credits held for them are released.');
  if (!ok) return;

  button.disabled = true;
  button.textContent = 'Stopping...';
  try {
    const result = await apiCall('POST', '/allocation/stop');
    appendLog({ type: 'retry', message: result.message });
    showAlert(alertBox, result.message, 'warning');
    // The worker needs a few seconds to notice, so let the normal status
    // flow report the stop rather than declaring it finished here.
  } catch (err) {
    showAlert(alertBox, err.message);
    button.disabled = false;
    button.textContent = 'Stop generating';
  }
});

/* --- Waiting to be picked up ------------------------------------------------ */

/**
 * Wait for a worker to claim this generation.
 *
 * A queued run has no attempt row until a worker takes it, which can be a
 * while behind a busy queue. This used to print "refresh in a moment" and
 * stop dead, so a page left open never recovered even once the run started -
 * and if no worker was running at all, the message gave no hint why.
 */
async function waitForAttempt() {
  const started = Date.now();
  const WARN_AFTER = 30000;
  const GIVE_UP_AFTER = 10 * 60 * 1000;

  for (;;) {
    const attempts = await apiCall(
      'GET', `/allocation/timetable/${timetableId}/attempts`);
    if (attempts.length) {
      hideAlert(alertBox);
      return attempts;
    }

    const waited = Date.now() - started;
    if (waited > GIVE_UP_AFTER) {
      showAlert(alertBox,
        'This generation has been waiting more than ten minutes without a '
        + 'worker picking it up. The queue may have no worker running.');
      document.getElementById('loading').hidden = true;
      return null;
    }

    document.getElementById('loading').textContent = waited > WARN_AFTER
      ? 'Still waiting for a worker to pick this up...'
      : 'Queued - waiting for a worker...';
    await new Promise((resolve) => setTimeout(resolve, 3000));
  }
}

/* --- Start ----------------------------------------------------------------- */

let allStages = [];
let lastStages = [];

(async () => {
  const me = await requireLogin();
  if (!me) return;

  document.getElementById('who').textContent =
    `${me.first_name} ${me.surname} - ${me.role}`;
  canStop = me.role === 'owner';

  if (!timetableId) {
    showAlert(alertBox, 'No timetable specified.');
    document.getElementById('loading').hidden = true;
    return;
  }

  // The retry loop creates an attempt per try, so take the most recent.
  const attempts = await waitForAttempt();
  if (!attempts) return;
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
  startElapsed(attempts[attempts.length - 1].started_at);

  const events = await apiCall('GET', `/allocation/${attemptId}/events`);
  events.forEach((e) => appendLog({ type: e.event_type, message: e.message }));

  document.getElementById('loading').hidden = true;
  document.getElementById('content').hidden = false;

  if (isDone(status)) {
    finish(status);
  } else {
    startStream();
  }
})().catch((err) => {
  document.getElementById('loading').hidden = true;
  showAlert(alertBox, err.message);
});
