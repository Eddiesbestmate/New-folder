/* Klasser - school users and invites. */

const alertBox = document.getElementById('alert');
const inviteAlert = document.getElementById('invite-alert');

let state = null;

document.getElementById('logout').addEventListener('click', logout);

function escapeHtml(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));
}

function when(value) {
  if (!value) return '';
  return new Date(value).toLocaleDateString(undefined,
    { day: 'numeric', month: 'short', year: 'numeric' });
}

/* --- Inviting -------------------------------------------------------------- */

document.getElementById('send').addEventListener('click', async () => {
  const email = document.getElementById('email').value.trim();
  if (!email.includes('@')) {
    showAlert(inviteAlert, 'Enter their email address.');
    return;
  }

  const button = document.getElementById('send');
  button.disabled = true;
  inviteAlert.hidden = true;

  try {
    const result = await apiCall('POST', '/users/invite', {
      email,
      role: document.getElementById('role').value,
    });

    document.getElementById('invite-result').hidden = false;
    document.getElementById('invite-message').textContent = result.emailed
      ? `Invite emailed to ${result.email}. The link works for `
        + `${result.expires_in_days} days.`
      : `Invite created for ${result.email}, but we could not email it - `
        + `send them this link yourself. It works for `
        + `${result.expires_in_days} days.`;
    document.getElementById('invite-link').textContent = result.accept_url;

    document.getElementById('email').value = '';
    await load();
  } catch (err) {
    showAlert(inviteAlert, err.message);
  } finally {
    button.disabled = false;
  }
});

document.getElementById('copy').addEventListener('click', async () => {
  const text = document.getElementById('invite-link').textContent;
  try {
    await navigator.clipboard.writeText(text);
    showAlert(alertBox, 'Link copied.', 'success');
  } catch {
    // Clipboard access is blocked in some browsers; the link is on screen
    // either way, so this is a convenience rather than the only route.
    showAlert(alertBox, 'Select the link above and copy it.');
  }
});

/* --- Rendering ------------------------------------------------------------- */

function renderInvites() {
  const card = document.getElementById('pending-card');
  card.hidden = !state.invites.length || !state.can_manage;
  if (card.hidden) return;

  document.getElementById('invite-rows').innerHTML = state.invites.map((i) => `
    <tr>
      <td>${escapeHtml(i.email)}</td>
      <td><span class="chip ${escapeHtml(i.role)}">${escapeHtml(i.role)}</span></td>
      <td class="small muted">${escapeHtml(i.invited_by || '')}</td>
      <td class="small ${i.expired ? 'muted' : ''}">
        ${i.expired ? 'expired' : when(i.expires_at)}
      </td>
      <td style="display:flex;gap:6px">
        <button class="btn small" data-resend="${i.id}">Resend</button>
        <button class="btn small" data-revoke="${i.id}"
                style="color:var(--text-danger)">Revoke</button>
      </td>
    </tr>`).join('');

  document.querySelectorAll('[data-resend]').forEach((btn) => {
    btn.addEventListener('click', async () => {
      try {
        const result = await apiCall(
          'POST', `/users/invites/${btn.dataset.resend}/resend`);
        document.getElementById('invite-result').hidden = false;
        document.getElementById('invite-message').textContent =
          `Invite for ${result.email} extended by ${result.expires_in_days} days.`;
        document.getElementById('invite-link').textContent = result.accept_url;
        await load();
      } catch (err) {
        showAlert(alertBox, err.message);
      }
    });
  });

  document.querySelectorAll('[data-revoke]').forEach((btn) => {
    btn.addEventListener('click', async () => {
      if (!confirm('Revoke this invite? The link stops working.')) return;
      try {
        await apiCall('DELETE', `/users/invites/${btn.dataset.revoke}`);
        showAlert(alertBox, 'Invite revoked.', 'success');
        await load();
      } catch (err) {
        showAlert(alertBox, err.message);
      }
    });
  });
}

function renderUsers() {
  document.getElementById('user-rows').innerHTML = state.users.map((u) => {
    const actions = [];
    if (state.can_manage && !u.is_you) {
      if (u.is_active) {
        actions.push(`<button class="btn small" data-role="${u.id}"
                        data-to="${u.role === 'owner' ? 'staff' : 'owner'}">
                        Make ${u.role === 'owner' ? 'staff' : 'owner'}</button>`);
        actions.push(`<button class="btn small" data-off="${u.id}"
                        style="color:var(--text-danger)">Deactivate</button>`);
      } else {
        actions.push(`<button class="btn small" data-on="${u.id}">Reactivate</button>`);
      }
    }
    return `
      <tr class="${u.is_active ? '' : 'inactive'}">
        <td>
          ${escapeHtml(u.first_name)} ${escapeHtml(u.surname)}
          ${u.is_you ? '<span class="small muted">(you)</span>' : ''}
        </td>
        <td class="small">${escapeHtml(u.email)}</td>
        <td><span class="chip ${escapeHtml(u.role)}">${escapeHtml(u.role)}</span></td>
        <td>
          ${u.is_active
            ? '<span class="chip">active</span>'
            : `<span class="chip off">deactivated ${when(u.deactivated_at)}</span>`}
        </td>
        <td style="display:flex;gap:6px;flex-wrap:wrap">${actions.join('')}</td>
      </tr>`;
  }).join('');

  document.querySelectorAll('[data-role]').forEach((btn) => {
    btn.addEventListener('click', async () => {
      try {
        await apiCall('PATCH', `/users/${btn.dataset.role}/role`,
          { role: btn.dataset.to });
        showAlert(alertBox, 'Role updated.', 'success');
        await load();
      } catch (err) {
        showAlert(alertBox, err.message);
      }
    });
  });

  document.querySelectorAll('[data-off]').forEach((btn) => {
    btn.addEventListener('click', async () => {
      if (!confirm('Deactivate this account? They lose access immediately. '
                   + 'You can turn it back on later.')) return;
      try {
        await apiCall('POST', `/users/${btn.dataset.off}/deactivate`);
        showAlert(alertBox, 'Account deactivated.', 'success');
        await load();
      } catch (err) {
        showAlert(alertBox, err.message);
      }
    });
  });

  document.querySelectorAll('[data-on]').forEach((btn) => {
    btn.addEventListener('click', async () => {
      try {
        await apiCall('POST', `/users/${btn.dataset.on}/reactivate`);
        showAlert(alertBox, 'Account reactivated.', 'success');
        await load();
      } catch (err) {
        showAlert(alertBox, err.message);
      }
    });
  });
}

/* --- Load ------------------------------------------------------------------ */

async function load() {
  state = await apiCall('GET', '/users');
  renderInvites();
  renderUsers();
}

(async () => {
  const me = await requireLogin();
  if (!me) return;

  document.getElementById('who').textContent =
    `${me.first_name} ${me.surname} - ${me.role}`;

  await load();

  if (!state.can_manage) {
    document.getElementById('invite-card').hidden = true;
  }

  document.getElementById('loading').hidden = true;
  document.getElementById('content').hidden = false;
})().catch((err) => {
  document.getElementById('loading').hidden = true;
  showAlert(alertBox, err.message);
});
