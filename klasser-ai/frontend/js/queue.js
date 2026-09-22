/* Klasser - generation queue.
 *
 * Polls rather than streams: unlike a single run's progress there is no SSE
 * channel for "what does this school have in flight", and the data is small
 * enough that a few seconds of staleness costs nothing.
 */

const alertBox = document.getElementById('alert');
const REFRESH_MS = 4000;

let timer = null;
let inFlight = false;

document.getElementById('logout').addEventListener('click', logout);

const LIVE = new Set(['queued', 'running']);

const STATUS_CLASS = {
  complete: 'pill-success',
  failed: 'pill-danger',
  cancelled: '',
  running: 'pill-accent',
  queued: 'pill-warning',
};

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));
}

function relative(iso) {
  if (!iso) return '';
  const seconds = Math.floor((Date.now() - new Date(iso).getTime()) / 1000);
  if (seconds < 60) return `${seconds}s ago`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
  return `${Math.floor(seconds / 86400)}d ago`;
}

function renderSummary(jobs, stats) {
  const mine = jobs.filter((j) => LIVE.has(j.status));
  const running = mine.filter((j) => j.status === 'running').length;
  const queued = mine.length - running;

  const parts = [];
  if (running) parts.push(`${running} running`);
  if (queued) parts.push(`${queued} waiting`);
  if (!parts.length) parts.push('Nothing running for this school');

  // Queue depth is platform-wide, so it only earns a mention when this
  // school is actually waiting behind it.
  if (queued && stats.queued > mine.length) {
    parts.push(`${stats.queued} queued across all schools`);
  }
  document.getElementById('summary').textContent = parts.join(' · ');
  document.getElementById('stop').hidden = mine.length === 0;
  return mine.length;
}

function renderJobs(jobs) {
  const body = document.getElementById('jobs');
  document.getElementById('empty').hidden = jobs.length > 0;

  body.innerHTML = jobs.map((job) => {
    const name = escapeHtml(job.timetable_name || '(removed)');
    const cls = STATUS_CLASS[job.status] || '';
    const attempts = `${job.attempts}/${job.max_attempts}`;
    const link = job.timetable_id
      ? `<a class="btn small" href="progress.html?timetable=${encodeURIComponent(job.timetable_id)}">View</a>`
      : '';
    // Clamped to two lines rather than left to wrap freely - a queue table
    // is meant to be scanned, and an unbounded pipeline error paragraph
    // (the kind layer3/layer4/layer5 raise) turned every failed row into a
    // wall of red text. The full reason is one click away via View.
    const error = job.last_error && job.status === 'failed'
      ? `<div class="small" style="color:var(--text-danger);max-width:44ch;
                  display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;
                  overflow:hidden" title="${escapeHtml(job.last_error)}">
           ${escapeHtml(job.last_error).slice(0, 200)}</div>`
      : '';
    return `
      <tr>
        <td>${name}${error}</td>
        <td><span class="pill ${cls}">${escapeHtml(job.status)}</span></td>
        <td>${attempts}</td>
        <td class="muted">${relative(job.created_at)}</td>
        <td style="text-align:right">${link}</td>
      </tr>`;
  }).join('');
}

async function refresh() {
  if (inFlight) return;
  inFlight = true;
  const pulse = document.getElementById('pulse');
  pulse.textContent = 'Updating…';
  try {
    const data = await apiCall('GET', '/allocation/queue');
    renderJobs(data.jobs);
    const live = renderSummary(data.jobs, data.queue);
    hideAlert(alertBox);
    pulse.textContent = live ? 'Live' : `Updated ${new Date()
      .toLocaleTimeString('en-AU', { hour: '2-digit', minute: '2-digit' })}`;
  } catch (err) {
    // Keep the last good table on screen; a blip should not blank the page.
    pulse.textContent = 'Reconnecting…';
    console.warn('queue refresh failed', err);
  } finally {
    inFlight = false;
  }
}

document.getElementById('stop').addEventListener('click', async () => {
  const ok = window.confirm(
    'Stop every generation this school has queued or running? Credits held '
    + 'for them are released.');
  if (!ok) return;

  const button = document.getElementById('stop');
  button.disabled = true;
  button.textContent = 'Stopping…';
  try {
    const result = await apiCall('POST', '/allocation/stop');
    showAlert(alertBox, result.message, 'success');
    await refresh();
  } catch (err) {
    showAlert(alertBox, err.message);
  } finally {
    button.disabled = false;
    button.textContent = 'Stop all generations';
  }
});

// Polling a tab nobody is looking at is wasted work, and on a laptop it is
// wasted battery. Pick straight back up when they return.
document.addEventListener('visibilitychange', () => {
  if (document.hidden) {
    clearInterval(timer);
    timer = null;
  } else if (!timer) {
    refresh();
    timer = setInterval(refresh, REFRESH_MS);
  }
});

(async () => {
  const me = await requireLogin();
  if (!me) return;

  document.getElementById('who').textContent =
    `${me.first_name} ${me.surname} - ${me.role}`;
  document.getElementById('stop').hidden = me.role !== 'owner';

  await refresh();
  document.getElementById('loading').hidden = true;
  document.getElementById('content').hidden = false;
  timer = setInterval(refresh, REFRESH_MS);
})().catch((err) => {
  document.getElementById('loading').hidden = true;
  showAlert(alertBox, err.message);
});
