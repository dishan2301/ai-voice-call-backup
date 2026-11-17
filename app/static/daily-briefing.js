(() => {
  let selected = null;
  const panel = document.createElement('section'); panel.className = 'card';
  panel.innerHTML = '<h3>Morning briefing</h3><label><input type="checkbox" data-enabled> Enable daily preparation</label>' +
    '<label>Local time <input type="time" data-time value="09:00"></label>' +
    '<label>Timezone <input data-zone value="Asia/Kolkata"></label>' +
    '<label>Priority sender emails (comma separated)<input data-vip></label>' +
    '<button type="button" data-save>Save briefing schedule</button><button type="button" data-recent>Open latest saved briefing</button>' +
    '<button type="button" data-play disabled>Play Briefing with Gemini Live</button><p role="status"></p><div data-sources></div>';
  document.querySelector('main').append(panel);
  const status = panel.querySelector('[role=status]');
  async function get(path) {
    const response = await fetch(apiUrl(path)); const value = await response.json();
    if (!response.ok) throw Error(typeof value.detail === 'string' ? value.detail : 'Request unavailable');
    return value;
  }
  get('/phone/daily/schedule').then(value => {
    panel.querySelector('[data-enabled]').checked = value.enabled;
    panel.querySelector('[data-time]').value = value.time;
    panel.querySelector('[data-zone]').value = value.timezone;
    panel.querySelector('[data-vip]').value = (value.vip_senders || []).join(', ');
    status.textContent = 'Preparation and Android alerts need separate activation. Background polling may be delayed; push is not configured.';
  }).catch(() => { status.textContent = 'Sign in to view the briefing schedule.'; });
  panel.querySelector('[data-save]').onclick = async () => {
    try {
      await request('/phone/daily/schedule', {enabled: panel.querySelector('[data-enabled]').checked,
        time: panel.querySelector('[data-time]').value, timezone: panel.querySelector('[data-zone]').value,
        vip_senders: panel.querySelector('[data-vip]').value.split(',').map(s => s.trim()).filter(Boolean)});
      status.textContent = 'Schedule saved. Preparation runs only when the server scheduler is activated.';
    } catch (error) { status.textContent = error.message; }
  };
  window.openDailyBriefing = async id => {
    if (!/^[a-f0-9]{32}$/.test(id)) return;
    sessionStorage.setItem('pending-daily-briefing', id);
    try {
      const value = await request('/phone/daily/runs/' + encodeURIComponent(id) + '/open', {session_key: sessionKey});
      selected = id;
      sessionStorage.removeItem('pending-daily-briefing');
      status.textContent = `Saved briefing from ${mailTime(value.generated_at)}. ${value.stale ? 'This snapshot may be stale.' : 'Sources verified at generation time.'}`;
      const document = value.document;
      panel.querySelector('[data-sources]').innerHTML = (document.items || []).map(item =>
        `<div class="card"><b>${esc(item.subject)}</b><p>${esc(item.sender)}</p><p>${esc(item.summary)}</p>${sourceLink(item)}${replyButton(item)}</div>`).join('') +
        (document.calendar || []).map(item => `<div class="card"><b>${esc(item.summary)}</b><p>${esc(JSON.stringify(item.start))}</p></div>`).join('');
      panel.querySelector('[data-play]').disabled = false;
    } catch (error) { selected = null; panel.querySelector('[data-play]').disabled = true; status.textContent = error.message; }
  };
  panel.querySelector('[data-sources]').onclick = event => {
    const button = event.target.closest('[data-reply]');
    if (button) draftReply(button);
  };
  panel.querySelector('[data-recent]').onclick = async () => {
    try {
      const result = await get('/phone/daily/runs');
      const latest = (result.runs || []).find(row => row.status === 'COMPLETE');
      if (latest) await window.openDailyBriefing(latest.id);
      else status.textContent = 'No completed morning briefing is available. Check schedule, sign-in and provider access.';
    } catch (error) { status.textContent = error.message; }
  };
  panel.querySelector('[data-play]').onclick = async () => {
    if (!selected) return;
    cancelReadback();
    await voice.start();
    for (let attempt = 0; attempt < 30; attempt++) {
      if (voice.playBriefing(selected)) { status.textContent = 'Requested playback of the saved briefing through Gemini Live.'; return; }
      await new Promise(resolve => setTimeout(resolve, 250));
    }
    status.textContent = 'Gemini Live is unavailable. The saved briefing remains on screen.';
  };
  const pending = sessionStorage.getItem('pending-daily-briefing');
  if (pending) window.openDailyBriefing(pending);
})();
