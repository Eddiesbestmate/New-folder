/* Klasser - transport fleet, routes and the solved schedule. */

const alertBox = document.getElementById('alert');
const busDialog = document.getElementById('bus-dialog');
const routeDialog = document.getElementById('route-dialog');
const busAlert = document.getElementById('bus-alert');
const routeAlert = document.getElementById('route-alert');

let campuses = [];
let buses = [];
let routes = [];
let editingBus = null;
let editingRoute = null;

document.getElementById('logout').addEventListener('click', logout);

function escapeHtml(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));
}

function campusOptions(selected) {
  return campuses.map((c) =>
    `<option value="${c.id}" ${c.id === selected ? 'selected' : ''}>`
    + `${escapeHtml(c.name)}</option>`).join('');
}

/* --- Overview -------------------------------------------------------------- */

function renderOverview(o) {
  document.getElementById('stats').innerHTML = [
    ['Campuses', o.campuses], ['Buses', o.buses],
    ['Total seats', o.seats], ['Routes', o.routes],
  ].map(([label, value]) => `
    <div class="card" style="padding:12px">
      <div style="font-size:22px;font-weight:700">${value}</div>
      <div class="small muted">${label}</div>
    </div>`).join('');

  const box = document.getElementById('readiness');
  if (o.ready) {
    box.className = 'alert alert-success small';
    box.textContent = 'Transport is set up. Cross-campus travel can be solved.';
    return;
  }

  const problems = [];
  if (!o.buses) problems.push('no buses in the fleet');
  if (o.missing_routes.length) {
    problems.push('no route for '
      + o.missing_routes.map((m) => `${m.from} to ${m.to}`).join(', '));
  }
  box.className = 'alert alert-warning small';
  box.innerHTML = `Transport is incomplete: ${escapeHtml(problems.join('; '))}.`
    + ' Students needing to cross campuses will have no way to travel.';
}

/* --- Fleet ----------------------------------------------------------------- */

function renderBuses() {
  document.getElementById('no-buses').hidden = buses.length > 0;
  document.getElementById('bus-rows').innerHTML = buses.map((b, i) => `
    <tr>
      <td><strong>${escapeHtml(b.name)}</strong></td>
      <td>${b.capacity}</td>
      <td>${escapeHtml(b.home_campus)}</td>
      <td><button class="btn small" data-bus="${i}">Edit</button></td>
    </tr>`).join('');

  document.querySelectorAll('[data-bus]').forEach((btn) => {
    btn.addEventListener('click', () => openBus(buses[Number(btn.dataset.bus)]));
  });
}

function openBus(bus) {
  editingBus = bus || null;
  busAlert.hidden = true;
  document.getElementById('bus-title').textContent = bus ? 'Edit bus' : 'Add bus';
  document.getElementById('bus-name').value = bus ? bus.name : '';
  document.getElementById('bus-capacity').value = bus ? bus.capacity : 45;
  document.getElementById('bus-campus').innerHTML =
    campusOptions(bus ? bus.home_campus_id : null);
  document.getElementById('bus-delete').hidden = !bus;
  busDialog.showModal();
}

document.getElementById('add-bus').addEventListener('click', () => openBus(null));
document.getElementById('bus-cancel')
  .addEventListener('click', () => busDialog.close());

document.getElementById('bus-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  busAlert.hidden = true;

  const body = {
    name: document.getElementById('bus-name').value.trim(),
    capacity: Number(document.getElementById('bus-capacity').value),
    home_campus_id: document.getElementById('bus-campus').value,
  };
  if (!body.name) {
    showAlert(busAlert, 'Give the bus a name.');
    return;
  }

  try {
    if (editingBus) await apiCall('PATCH', `/transport/buses/${editingBus.id}`, body);
    else await apiCall('POST', '/transport/buses', body);
    busDialog.close();
    await reload();
  } catch (err) {
    showAlert(busAlert, err.message);
  }
});

document.getElementById('bus-delete').addEventListener('click', async () => {
  if (!editingBus || !confirm(`Remove ${editingBus.name}?`)) return;
  try {
    await apiCall('DELETE', `/transport/buses/${editingBus.id}`);
    busDialog.close();
    await reload();
  } catch (err) {
    // Buses used by a timetable cannot be removed; the message explains why.
    showAlert(busAlert, err.message);
  }
});

/* --- Routes ---------------------------------------------------------------- */

function renderRoutes() {
  document.getElementById('no-routes').hidden = routes.length > 0;
  document.getElementById('route-rows').innerHTML = routes.map((r, i) => `
    <tr>
      <td>${escapeHtml(r.from_campus)}</td>
      <td>${escapeHtml(r.to_campus)}</td>
      <td>${r.travel_minutes} min</td>
      <td><button class="btn small" data-route="${i}">Edit</button></td>
    </tr>`).join('');

  document.querySelectorAll('[data-route]').forEach((btn) => {
    btn.addEventListener('click', () => openRoute(routes[Number(btn.dataset.route)]));
  });
}

function openRoute(route) {
  editingRoute = route || null;
  routeAlert.hidden = true;
  document.getElementById('route-title').textContent =
    route ? 'Edit route' : 'Add route';
  document.getElementById('route-from').innerHTML =
    campusOptions(route ? route.from_campus_id : null);
  document.getElementById('route-to').innerHTML =
    campusOptions(route ? route.to_campus_id : (campuses[1] || {}).id);
  document.getElementById('route-minutes').value =
    route ? route.travel_minutes : 12;

  // Direction is fixed once a route exists - only the time is editable.
  document.getElementById('route-from').disabled = Boolean(route);
  document.getElementById('route-to').disabled = Boolean(route);
  document.getElementById('both-ways-field').hidden = Boolean(route);
  document.getElementById('route-delete').hidden = !route;

  routeDialog.showModal();
}

document.getElementById('add-route')
  .addEventListener('click', () => openRoute(null));
document.getElementById('route-cancel')
  .addEventListener('click', () => routeDialog.close());

document.getElementById('route-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  routeAlert.hidden = true;

  const body = {
    from_campus_id: document.getElementById('route-from').value,
    to_campus_id: document.getElementById('route-to').value,
    travel_minutes: Number(document.getElementById('route-minutes').value),
  };

  if (!editingRoute && body.from_campus_id === body.to_campus_id) {
    showAlert(routeAlert, 'A route must go between two different campuses.');
    return;
  }

  try {
    if (editingRoute) {
      await apiCall('PATCH', `/transport/routes/${editingRoute.id}`, body);
    } else {
      const both = document.getElementById('both-ways').checked;
      await apiCall('POST', '/transport/routes', body, { both_ways: both });
    }
    routeDialog.close();
    await reload();
  } catch (err) {
    showAlert(routeAlert, err.message);
  }
});

document.getElementById('route-delete').addEventListener('click', async () => {
  if (!editingRoute || !confirm('Remove this route?')) return;
  try {
    await apiCall('DELETE', `/transport/routes/${editingRoute.id}`);
    routeDialog.close();
    await reload();
  } catch (err) {
    showAlert(routeAlert, err.message);
  }
});

/* --- Schedule -------------------------------------------------------------- */

async function loadVersions() {
  const timetables = await apiCall('GET', '/timetables');
  const withVersions = timetables.filter((t) => t.latest_version);

  document.getElementById('version').innerHTML = withVersions.length
    ? withVersions.map((t) =>
        `<option value="${t.latest_version}">${escapeHtml(t.name)}</option>`).join('')
    : '<option value="">No generated timetables yet</option>';

  if (withVersions.length) await loadSchedule();
  else document.getElementById('chains').innerHTML =
    '<p class="muted">Generate a timetable to see the bus schedule.</p>';
}

async function loadSchedule() {
  const versionId = document.getElementById('version').value;
  if (!versionId) return;

  const daySelect = document.getElementById('day');
  const day = daySelect.value;
  const data = await apiCall('GET', `/transport/schedule/${versionId}`,
                             null, { day });

  if (!daySelect.dataset.filled) {
    daySelect.innerHTML = '<option value="">All days</option>'
      + data.days.map((d) => `<option value="${d}">Day ${d}</option>`).join('');
    daySelect.dataset.filled = '1';
  }

  document.getElementById('schedule-stats').textContent =
    `${data.movements} movements, ${data.passengers} passenger trips, `
    + `${data.empty_legs} empty legs (${data.empty_leg_pct}%)`;

  if (!data.chains.length) {
    document.getElementById('chains').innerHTML =
      '<p class="muted">No bus movements - no student needed to change campus.</p>';
    return;
  }

  document.getElementById('chains').innerHTML = data.chains.map((c) => `
    <div class="chain">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
        <strong>${escapeHtml(c.bus)} &mdash; Day ${c.day}</strong>
        <span class="pill">${c.passengers} passengers &middot; ${c.empty_legs} empty</span>
      </div>
      ${c.movements.map((m) => `
        <div class="leg ${m.is_empty_leg ? 'empty' : ''}">
          <span class="time">${m.departure}</span>
          <span>${escapeHtml(m.from_campus)} &rarr; ${escapeHtml(m.to_campus)}</span>
          <span class="muted">${m.is_empty_leg
            ? 'repositioning'
            : `${m.passenger_count}/${c.capacity}`}</span>
        </div>`).join('')}
    </div>`).join('');
}

document.getElementById('version').addEventListener('change', () => {
  document.getElementById('day').dataset.filled = '';
  loadSchedule().catch((err) => showAlert(alertBox, err.message));
});
document.getElementById('day').addEventListener('change', () => {
  loadSchedule().catch((err) => showAlert(alertBox, err.message));
});

/* --- Tabs ------------------------------------------------------------------ */

document.querySelectorAll('.tab').forEach((tab) => {
  tab.addEventListener('click', async () => {
    document.querySelectorAll('.tab').forEach((t) => t.classList.remove('active'));
    tab.classList.add('active');
    for (const name of ['fleet', 'routes', 'schedule']) {
      document.getElementById(`panel-${name}`).hidden = name !== tab.dataset.tab;
    }
    if (tab.dataset.tab === 'schedule') {
      try {
        await loadVersions();
      } catch (err) {
        showAlert(alertBox, err.message);
      }
    }
  });
});

/* --- Load ------------------------------------------------------------------ */

async function reload() {
  const [overview, busList, routeList] = await Promise.all([
    apiCall('GET', '/transport/overview'),
    apiCall('GET', '/transport/buses'),
    apiCall('GET', '/transport/routes'),
  ]);

  buses = busList;
  routes = routeList;
  renderOverview(overview);
  renderBuses();
  renderRoutes();
}

/* --- Crossing campuses mid-day --------------------------------------------- */

async function loadCrossCampus(me) {
  const box = document.getElementById('allow-cross');
  const save = document.getElementById('save-cross');
  const prefs = await apiCall('GET', '/transport/preferences');

  box.checked = prefs.allow_cross_campus_travel;

  // Say what the rule actually means for this school's own travel time,
  // rather than describing it in the abstract.
  const minutes = prefs.shortest_route_minutes;
  document.getElementById('cross-detail').textContent = minutes
    ? `A class at the other campus is only scheduled when the break before it `
      + `is long enough to travel - at least ${minutes} minutes here. `
      + `Left off, every student stays at one campus for the whole day.`
    : 'Add a route between your campuses first, so we know how long the trip '
      + 'takes.';

  const readOnly = me.role !== 'owner';
  box.disabled = readOnly;
  save.hidden = readOnly;
  if (readOnly) {
    document.getElementById('cross-note').textContent =
      'Only the owner can change this.';
  }
}

document.getElementById('save-cross').addEventListener('click', async () => {
  const save = document.getElementById('save-cross');
  const note = document.getElementById('cross-note');
  save.disabled = true;
  note.textContent = 'Saving...';
  try {
    const result = await apiCall('PUT', '/transport/preferences', {
      allow_cross_campus_travel: document.getElementById('allow-cross').checked,
    });
    note.textContent = result.message;
  } catch (err) {
    note.textContent = '';
    showAlert(alertBox, err.message);
  } finally {
    save.disabled = false;
  }
});

(async () => {
  const me = await requireLogin();
  if (!me) return;

  document.getElementById('who').textContent =
    `${me.first_name} ${me.surname} - ${me.role}`;

  campuses = await apiCall('GET', '/schools/campuses');
  const overview = await apiCall('GET', '/transport/overview');

  document.getElementById('intro').textContent =
    'Buses and routes feed the transport solver, which works out who needs to '
    + 'travel between campuses and when.';

  if (!overview.applies) {
    document.getElementById('not-applicable').hidden = false;
  } else {
    document.getElementById('main').hidden = false;
    await loadCrossCampus(me);
    await reload();
  }

  document.getElementById('loading').hidden = true;
  document.getElementById('content').hidden = false;
})().catch((err) => {
  document.getElementById('loading').hidden = true;
  showAlert(alertBox, err.message);
});
