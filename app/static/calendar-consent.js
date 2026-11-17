(async () => {
  const values = new URLSearchParams(location.search);
  const payload = {code: values.get('code') || '', state: values.get('state') || '', error: values.get('error') || ''};
  history.replaceState(null, '', location.pathname);
  const status = document.querySelector('#status');
  try {
    const response = await fetch('/auth/calendar/finish', {method: 'POST',
      headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload)});
    const result = await response.json();
    status.textContent = response.ok ? 'Calendar connected. Return to Morning Mail.' :
      (typeof result.detail === 'string' ? result.detail : 'Calendar consent could not be verified. Sign in and try again.');
  } catch (_) { status.textContent = 'Calendar consent could not finish. Sign in and try again.'; }
})();
