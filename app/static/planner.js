/* Calendar/reminder approvals remain separate from mail and WhatsApp approvals. */
(() => {
  let proposal = null, polling = false;
  const panel = document.createElement('section');
  panel.className = 'card';
  panel.innerHTML = '<h3>Calendar and reminders</h3><p>Ask about Calendar, or say “remind me”. Review changes here before confirming.</p><pre style="white-space:pre-wrap"></pre><button type="button" data-confirm hidden>Confirm exact operation</button><button type="button" data-enable>Enable reminder notifications</button><p role="status"></p>';
  document.querySelector('main').append(panel);
  const details = panel.querySelector('pre'), status = panel.querySelector('[role=status]');
  const confirmButton = panel.querySelector('[data-confirm]');
  window.clearPlannerReview = () => { proposal = null; confirmButton.hidden = true; };
  window.showPlannerResult = result => {
    window.clearPlannerReview();
    window.plannerActive = !!(result.calendar || result.reminders || result.action_id);
    if (!result.action_id || result.state !== 'REVIEW') return;
    proposal = result;
    details.textContent = JSON.stringify(result.details, null, 2);
    confirmButton.hidden = false;
    status.textContent = 'Review every event or reminder detail, including timezone. Confirm only this exact version.';
  };
  confirmButton.onclick = async () => {
    if (!proposal) return;
    const current = proposal; proposal = null; confirmButton.hidden = true;
    try {
      const result = await request('/phone/planner/confirm', {session_key: sessionKey, action_id: current.action_id,
        confirmation_token: current.confirmation_token, confirmation: 'confirm'});
      status.textContent = result.answer || result.state;
    } catch (error) { status.textContent = error.message; }
  };
  panel.querySelector('[data-enable]').onclick = async () => {
    if (!('Notification' in window) || !('serviceWorker' in navigator)) {
      status.textContent = 'System notifications are unavailable here. Due reminders remain saved for when you return.'; return;
    }
    try {
      const permission = await Notification.requestPermission();
      if (permission !== 'granted') { status.textContent = 'Notification permission denied. Reminders remain saved.'; return; }
      await navigator.serviceWorker.register('/static/planner-sw.js');
      status.textContent = 'Notifications enabled while this page can check reminders. Closed-app push delivery is not configured.';
      poll();
    } catch (_) { status.textContent = 'Notifications could not start. Reminders remain saved.'; }
  };
  async function poll() {
    if (polling || document.hidden || !('Notification' in window) || Notification.permission !== 'granted') return;
    polling = true;
    try {
      const registration = await navigator.serviceWorker.getRegistration('/static/');
      if (!registration) return;
      const result = await request('/phone/planner/notifications/claim', {});
      for (const item of result.notifications || []) {
        let delivery = 'SUBMITTED';
        try { await registration.showNotification('Morning Mail reminder', {body: item.title, tag: item.id}); }
        catch (_) { delivery = 'FAILED'; }
        await request('/phone/planner/notifications/ack', {reminder_id: item.id, delivery_token: item.delivery_token, status: delivery});
        status.textContent = delivery === 'SUBMITTED' ? 'Reminder submitted to system notifications; viewing is not confirmed.' : 'Notification failed; the reminder remains due.';
      }
    } catch (_) { status.textContent = 'Cannot check reminders right now. Saved reminders will be checked when connected.'; }
    finally { polling = false; }
  }
  setInterval(poll, 30000);
  document.addEventListener('visibilitychange', poll);
})();
