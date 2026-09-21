/* Klasser AI - authentication.
 *
 * Password login for now. Google and Microsoft are added by calling
 * signInWithOAuth() below once the providers are enabled in the Supabase
 * dashboard - nothing else here changes, because every method yields the same
 * Supabase session.
 */

/* --- Session helpers ------------------------------------------------------ */

async function currentSession() {
  const { data } = await supabaseClient.auth.getSession();
  return data.session;
}

/* Redirect to login unless signed in. Returns the /auth/me profile. */
async function requireLogin(redirectTo = 'login.html') {
  const session = await currentSession();
  if (!session) {
    window.location.href = redirectTo;
    return null;
  }
  try {
    return await apiCall('GET', '/auth/me');
  } catch (err) {
    if (err.status === 401 || err.status === 403) {
      await supabaseClient.auth.signOut();
      window.location.href = redirectTo;
      return null;
    }
    throw err;
  }
}

/* Send an already-signed-in visitor straight to the app. */
async function redirectIfSignedIn(target = 'dashboard.html') {
  const session = await currentSession();
  if (session) window.location.href = target;
}

/* --- Login / logout ------------------------------------------------------- */

async function loginWithPassword(email, password) {
  const { error } = await supabaseClient.auth.signInWithPassword({
    email: email.trim(),
    password,
  });

  if (error) {
    // Supabase returns the same message for a bad password and an unknown
    // address, which is correct - it avoids revealing which emails exist.
    if (/invalid login credentials/i.test(error.message)) {
      throw new Error('Incorrect email or password.');
    }
    if (/email not confirmed/i.test(error.message)) {
      throw new Error('Confirm your email address before logging in.');
    }
    throw new Error(error.message);
  }

  // Confirms the account is provisioned in our schema, not just in Supabase.
  return apiCall('GET', '/auth/me');
}

async function devLogin(email, password) {
  await loginWithPassword(email, password);
  try {
    await apiCall('GET', '/auth/check-dev');
  } catch (err) {
    await supabaseClient.auth.signOut();
    throw new Error('Access restricted to authorised team members.');
  }
}

async function logout() {
  await supabaseClient.auth.signOut();
  window.location.href = 'index.html';
}

/* --- Signup --------------------------------------------------------------- */

async function signupSchool(payload) {
  await apiCall('POST', '/auth/signup', payload);
  // The backend created the account; sign in to get a session.
  return loginWithPassword(payload.email, payload.password);
}

/* --- OAuth (enable in Supabase dashboard, then call these) ----------------- */

async function loginWithProvider(provider) {
  const { error } = await supabaseClient.auth.signInWithOAuth({
    provider, // 'google' | 'azure'
    options: { redirectTo: `${window.location.origin}/dashboard.html` },
  });
  if (error) throw new Error(error.message);
}

/* --- Small UI helper ------------------------------------------------------ */

function showAlert(el, message, kind = 'error') {
  el.className = `alert alert-${kind}`;
  el.textContent = message;
  el.hidden = false;
}

function hideAlert(el) {
  el.hidden = true;
}
