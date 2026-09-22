/* Klasser - accept an invite and join a school.

   The only signed-out page that writes anything. The token in the URL is the
   credential; everything else on the form is the invitee describing themselves. */

const alertBox = document.getElementById('alert');
const params = new URLSearchParams(window.location.search);
const token = params.get('token');

let invite = null;

function dead(message) {
  document.getElementById('loading').hidden = true;
  document.getElementById('content').hidden = true;
  document.getElementById('dead').hidden = false;
  document.getElementById('dead-detail').textContent = message;
}

document.getElementById('join').addEventListener('click', async () => {
  const firstName = document.getElementById('first-name').value.trim();
  const surname = document.getElementById('surname').value.trim();
  const password = document.getElementById('password').value;

  if (!firstName || !surname) {
    showAlert(alertBox, 'Tell us your name.');
    return;
  }
  if (password.length < 8) {
    showAlert(alertBox, 'Your password needs at least 8 characters.');
    return;
  }
  if (!/[A-Za-z]/.test(password) || !/\d/.test(password)) {
    showAlert(alertBox, 'Your password needs at least one letter and one number.');
    return;
  }

  const button = document.getElementById('join');
  button.disabled = true;
  button.textContent = 'Joining...';
  alertBox.hidden = true;

  try {
    await apiCall('POST', '/users/accept', {
      token, first_name: firstName, surname, password,
    });

    // Sign them straight in with the password they just chose, so joining ends
    // on the dashboard rather than at another login form. If that fails the
    // account still exists, so send them to log in rather than showing an
    // error for something that worked.
    try {
      await loginWithPassword(invite.email, password);
      window.location.href = 'dashboard.html';
    } catch {
      showAlert(alertBox,
        'Your account is ready. Log in with your new password.', 'success');
      setTimeout(() => { window.location.href = 'login.html'; }, 1800);
    }
  } catch (err) {
    showAlert(alertBox, err.message);
    button.disabled = false;
    button.textContent = 'Join';
  }
});

(async () => {
  if (!token) {
    dead('That link is missing its invitation code. Ask whoever invited you to '
         + 'send it again.');
    return;
  }

  try {
    invite = await apiCall('GET', `/users/invite/${encodeURIComponent(token)}`);
  } catch (err) {
    dead(err.message);
    return;
  }

  document.getElementById('heading').textContent = `Join ${invite.school}`;
  document.getElementById('subtitle').textContent =
    `You have been invited as ${invite.role === 'owner' ? 'an owner' : 'staff'}.`
    + ' Set your name and a password to finish.';
  document.getElementById('email').value = invite.email;

  document.getElementById('loading').hidden = true;
  document.getElementById('content').hidden = false;
  document.getElementById('first-name').focus();
})();
