/* Klasser AI - API access.
 *
 * Every call to the backend goes through apiCall(), which attaches the current
 * Supabase access token and turns error responses into a thrown ApiError with a
 * message fit to show a user.
 */

const supabaseClient = window.supabase.createClient(
  KLASSER.SUPABASE_URL,
  KLASSER.SUPABASE_ANON_KEY
);

class ApiError extends Error {
  constructor(message, status) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
  }
}

async function accessToken() {
  const { data } = await supabaseClient.auth.getSession();
  return data.session ? data.session.access_token : null;
}

/* `params` keeps query strings out of the path literal, so the path stays a
 * plain string that check_frontend_calls.py can verify against the real
 * routes. Null and undefined values are dropped rather than sent as "null". */
async function apiCall(method, path, body = null, params = null) {
  const token = await accessToken();

  const headers = { 'Content-Type': 'application/json' };
  if (token) headers['Authorization'] = `Bearer ${token}`;

  let url = `${KLASSER.API_BASE}${path}`;
  if (params) {
    const query = new URLSearchParams();
    for (const [key, value] of Object.entries(params)) {
      if (value !== null && value !== undefined && value !== '') {
        query.append(key, value);
      }
    }
    const encoded = query.toString();
    if (encoded) url += (path.includes('?') ? '&' : '?') + encoded;
  }

  let res;
  try {
    res = await fetch(url, {
      method,
      headers,
      body: body ? JSON.stringify(body) : null,
    });
  } catch (err) {
    throw new ApiError(
      'Could not reach the server. Is the backend running on port 8000?',
      0
    );
  }

  if (res.status === 204) return null;

  let payload = null;
  try {
    payload = await res.json();
  } catch {
    /* empty or non-JSON body */
  }

  if (!res.ok) {
    throw new ApiError(errorMessage(payload, res.status), res.status);
  }
  return payload;
}

/* FastAPI reports errors as a string detail, or as a list of validation
 * objects. Flatten both into one readable line. */
function errorMessage(payload, status) {
  const detail = payload && payload.detail;

  if (typeof detail === 'string') return detail;

  if (Array.isArray(detail)) {
    return detail
      .map((e) => {
        const field = Array.isArray(e.loc) ? e.loc[e.loc.length - 1] : '';
        return field ? `${field}: ${e.msg}` : e.msg;
      })
      .join('. ');
  }

  return `Request failed (HTTP ${status})`;
}

/* Format a UTC timestamp in the school's local timezone (TIMEZONE.md). */
function formatLocalTime(utcString, schoolTimezone) {
  if (!utcString) return '';
  return new Date(utcString).toLocaleString('en-AU', {
    timeZone: schoolTimezone || 'Australia/Sydney',
    dateStyle: 'medium',
    timeStyle: 'short',
  });
}

supabaseClient.auth.onAuthStateChange((event) => {
  if (event === 'SIGNED_OUT') {
    const open = ['/index.html', '/login.html', '/signup.html', '/dev-login.html', '/'];
    if (!open.some((p) => window.location.pathname.endsWith(p))) {
      window.location.href = 'index.html';
    }
  }
});
