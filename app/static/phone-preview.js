let spoken = '', savedAt = '', draft = null, selectedReply = null, reviewTicket = '', readbackGeneration = 0, currentUtterance = null, reconnectTimer = null, reconnectAttempts = 0;
let nativeSpeech = null;
let browserSpeechFallback = false;
let whatsapp = JSON.parse(sessionStorage.getItem('whatsapp-draft') || '{}');
const saveWhatsApp = () => sessionStorage.setItem('whatsapp-draft', JSON.stringify(whatsapp));
const whatsappFingerprint = () => JSON.stringify([whatsapp.name || '', whatsapp.number || '', whatsapp.message || '']);
const whatsappMode = active => { if (voice) voice.contactMode(active); };
function stopSpeech() {
  nativeSpeech = null;
  if (window.MorningMailSpeech) window.MorningMailSpeech.stop();
  if (window.speechSynthesis) window.speechSynthesis.cancel();
}
window.mailSpeechComplete = (id, success) => {
  if (!nativeSpeech || nativeSpeech.id !== id) return;
  const next = nativeSpeech; nativeSpeech = null;
  if (success) next.done();
  else if (!browserSpeechFallback && window.speechSynthesis) {
    browserSpeechFallback = true;
    $('#voice-status').textContent = 'Phone speech unavailable; using browser voice…';
    say(next.text, next.done, true, true);
  } else {
    cancelReadback(); $('#voice-status').textContent = 'Speech playback failed. Check phone media volume and text-to-speech settings.';
  }
};
const accessCode = new URLSearchParams(window.location.search).get('code') || '';
const apiUrl = path => `${path}${path.includes('?') ? '&' : '?'}code=${encodeURIComponent(accessCode)}`;
const random = new Uint8Array(24); crypto.getRandomValues(random);
let sessionKey = sessionStorage.getItem('mail-session') || Array.from(random, b => b.toString(16).padStart(2, '0')).join('');
sessionStorage.setItem('mail-session', sessionKey);
const $ = selector => document.querySelector(selector);
const esc = value => String(value == null ? '' : value).replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
const mailTime = value => {
  if (!value) return '';
  const raw = String(value);
  const date = new Date(raw + (/[zZ]$|[+-]\d\d:\d\d$/.test(raw) ? '' : 'Z'));
  return Number.isNaN(date.getTime()) ? raw : new Intl.DateTimeFormat('en-IN', {
    timeZone: 'Asia/Kolkata', day: 'numeric', month: 'short', year: 'numeric',
    hour: 'numeric', minute: '2-digit', hour12: true
  }).format(date) + ' IST';
};
const voice = new MailVoiceSession(apiUrl, sessionKey, event => {
  if (event.type === 'session.state') {
    $('#voice-status').textContent = event.state.toLowerCase();
    if (event.state === 'LISTENING') reconnectAttempts = 0;
  }
  if (event.type === 'session.error') {
    $('#voice-status').textContent = event.message; cancelReadback(); recover();
    if (/voice disconnected|voice connection failed|session ended/i.test(event.message || '') && !reconnectTimer && reconnectAttempts < 2) {
      reconnectAttempts++;
      $('#voice-status').textContent = `Voice connection lost. Reconnecting (${reconnectAttempts}/2)…`;
      reconnectTimer = setTimeout(() => { reconnectTimer = null; voice.start(); }, reconnectAttempts * 1000);
    } else if (reconnectAttempts >= 2) {
      $('#voice-status').textContent = 'Voice could not reconnect. Check the network, then tap Ask or use typed chat.';
    }
  }
  if (event.type === 'speech.error') $('#voice-status').textContent = event.message;
  if (event.type === 'speech.fallback' && event.text) {
    $('#voice-status').textContent = 'Speaking answer…';
    say(event.text, () => { $('#voice-status').textContent = 'listening'; });
  }
  if (event.type === 'transcript.partial' || event.type === 'transcript.final') $('#transcript').textContent = event.text;
  if (event.type === 'tool.started') $('#voice-status').textContent = 'Checking your mail…';
  if (event.type === 'mail.result') {
    showResult(event.result);
  }
  if (event.type === 'response.cancelled') cancelReadback();
  if (event.type === 'contact.request') handleWhatsAppCommand(event.text);
});

function whatsappStatus(text) { $('#answer').textContent = text; $('#answer').scrollIntoView({behavior: 'smooth'}); }
function searchContact(name) {
  if (!window.MorningMailContacts) { whatsappStatus('Contact search is available only in the Android app.'); return; }
  const id = String(Date.now()); whatsapp.pending = id; whatsapp.query = name; whatsapp.reviewed = '';
  saveWhatsApp(); whatsappMode(true); $('#voice-status').textContent = `Searching contacts for ${name}…`;
  window.MorningMailContacts.search(name, id);
}
window.mailContactsResult = (id, status, matches) => {
  if (whatsapp.pending !== id) return;
  delete whatsapp.pending;
  if (status === 'permission_denied') { whatsappStatus('Contacts permission was denied. Allow it in Android settings or choose a contact manually in WhatsApp.'); return; }
  if (status !== 'ok' || !matches.length) { whatsappStatus(`I could not find a contact matching ${whatsapp.query || 'that name'}.`); return; }
  const exact = matches.filter(row => row.exact);
  if (exact.length === 1) return selectWhatsAppContact(exact[0]);
  if (matches.length === 1) return selectWhatsAppContact(matches[0]);
  whatsappStatus(`I found ${matches.length} contacts. Choose the exact recipient.`);
  $('#evidence').innerHTML = matches.map((row, index) => `<button type="button" class="choice" data-wa-index="${index}">${esc(row.name)} · ${esc(row.number)}</button>`).join('');
  whatsapp.matches = matches; saveWhatsApp(); say(`I found ${matches.length} contacts. Please choose the exact recipient on screen.`);
};
function selectWhatsAppContact(row) {
  window.plannerActive = false;
  whatsapp.name = row.name; whatsapp.number = row.number; whatsapp.matches = []; whatsapp.reviewed = ''; saveWhatsApp();
  whatsappStatus(`WhatsApp recipient selected: ${row.name} · ${row.number}. What should the message say?`);
  $('#evidence').innerHTML = ''; whatsappMode(true);
}
async function rewriteWhatsApp(instruction) {
  if (!whatsapp.message) { whatsappStatus('Tell me the WhatsApp message first.'); return; }
  const original = whatsappFingerprint();
  const token = String(Date.now()) + Math.random();
  whatsapp.rewriteToken = token; whatsapp.reviewed = ''; saveWhatsApp();
  whatsappStatus('Gemini is rewriting your WhatsApp draft…');
  try {
    const result = await request('/phone/whatsapp/rewrite', {message: whatsapp.message, instruction});
    if (whatsapp.rewriteToken !== token || whatsappFingerprint() !== original) return;
    whatsapp.message = result.message; whatsapp.reviewed = ''; delete whatsapp.rewriteToken; saveWhatsApp();
    const review = `WhatsApp to ${whatsapp.name}, ${whatsapp.number}. Message: ${whatsapp.message}`;
    whatsappStatus(review + ' Review the revised message, then say read it before send now.');
    say(review); // Automatic playback never authorizes handoff; explicit fresh review is required.
  } catch (_) {
    if (whatsapp.rewriteToken !== token || whatsappFingerprint() !== original) return;
    delete whatsapp.rewriteToken; saveWhatsApp();
    whatsappStatus('Gemini could not rewrite the message. Your original draft is unchanged. Say read it to review again.');
  }
}
function handleWhatsAppCommand(raw) {
  const text = String(raw || '').trim();
  const target = text.match(/(?:whats\s*app(?: message)? (?:to|for)|व्हाट्स(?:ऐप|एप)(?: पर)?(?: संदेश)?|(?:ko|को) whatsapp)\s+(.+?)(?=\s+(?:tell|saying|say|message|bolo|कहो|बोलो)\b|[,.]|$)/i);
  const body = text.match(/(?:tell (?:him|her|them)|saying|say|message(?: is| saying)?|bolo|कहो|बोलो)\s+(.+)$/i);
  if (target) { whatsapp = {query: target[1].trim(), message: body ? body[1].trim() : '', reviewed: ''}; saveWhatsApp(); searchContact(whatsapp.query); return true; }
  if (!whatsapp.name) { whatsappStatus('Which WhatsApp contact do you mean? Say the contact name.'); return true; }
  if (/\b(?:professional|formal|shorter|short|politely|polite|chhota|vinamra)\b|पेशेवर|छोटा|संक्षिप्त|विनम्र|नम्र/i.test(text)) {
    rewriteWhatsApp(text); return true;
  }
  const change = text.match(/(?:change (?:the )?message(?: to)?|tell (?:him|her|them)|bolo|कहो|बोलो)\s+(.+)$/i);
  if (change) { whatsapp.message = change[1].trim(); whatsapp.reviewed = ''; saveWhatsApp(); whatsappStatus(`Updated WhatsApp draft for ${whatsapp.name}.`); return true; }
  if (/\b(?:read it|read message|padh(?:o| do)?)\b|पढ़ो/i.test(text)) {
    if (!whatsapp.message) { whatsappStatus('Tell me the WhatsApp message first.'); return true; }
    const review = `WhatsApp to ${whatsapp.name}, ${whatsapp.number}. Message: ${whatsapp.message}`;
    const fingerprint = whatsappFingerprint();
    whatsapp.reviewed = ''; saveWhatsApp(); whatsappStatus(review);
    say(review, () => {
      if (whatsappFingerprint() === fingerprint && !whatsapp.rewriteToken) {
        whatsapp.reviewed = fingerprint; saveWhatsApp();
      }
    }); return true;
  }
  if (/\b(?:send now|open whatsapp|bhej(?:o| do)?)\b|भेजो/i.test(text)) {
    if (whatsapp.rewriteToken || !whatsapp.message || whatsapp.reviewed !== whatsappFingerprint()) {
      whatsappStatus('Review the exact WhatsApp recipient and message first by saying read it.'); return true;
    }
    const id = String(Date.now()); whatsapp.handoff = id; saveWhatsApp();
    window.MorningMailContacts.openWhatsApp(whatsapp.number, whatsapp.message, id); return true;
  }
  if (body) { whatsapp.message = body[1].trim(); whatsapp.reviewed = ''; saveWhatsApp(); whatsappStatus(`WhatsApp draft prepared for ${whatsapp.name}. Say read it to review.`); return true; }
  whatsappStatus('Tell me the message, ask to revise it, say read it, or say send now.'); return true;
}
window.mailWhatsAppResult = (id, status) => {
  if (whatsapp.handoff !== id) return;
  delete whatsapp.handoff; saveWhatsApp();
  if (status === 'chat_opened') whatsappStatus('WhatsApp chat opened with the reviewed message. Complete sending in WhatsApp; delivery is not confirmed.');
  else if (status === 'not_installed') whatsappStatus('WhatsApp is not installed. Nothing was sent.');
  else if (status === 'invalid') whatsappStatus('That contact number is invalid for WhatsApp. Nothing was sent.');
  else whatsappStatus('WhatsApp could not open or the handoff was cancelled. Nothing was confirmed sent.');
};
async function recover() {
  try { const result = await request(`/phone/resume?session_key=${encodeURIComponent(sessionKey)}`); if (result.state && result.state !== 'EMPTY') showResult(result); }
  catch (_) {}
}
async function request(path, payload) {
  const response = await fetch(apiUrl(path), payload === undefined ? {} : {
    method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload)});
  const result = await response.json();
  if (!response.ok) throw Error(typeof result.detail === 'string' ? result.detail : 'The request could not be completed.');
  return result;
}
function sourceLink(item) {
  if (item.provider === 'microsoft') {
    try {
      const source = new URL(item.source_url);
      if (source.protocol === 'https:' && ['outlook.live.com', 'outlook.office.com', 'outlook.office365.com'].includes(source.hostname))
        return `<a href="${esc(source.href)}" target="_blank" rel="noopener noreferrer">Open source email</a>`;
    } catch (_) {}
    return '';
  }
  return item.message_id ? `<a href="https://mail.google.com/mail/u/0/#all/${encodeURIComponent(item.message_id)}" target="_blank" rel="noopener noreferrer">Open source email</a>` : '';
}
function replyButton(item) {
  return item.message_id ? `<button type="button" class="choice" data-reply data-message-id="${esc(item.message_id)}" data-thread-id="${esc(item.thread_id)}" data-sender="${esc(item.sender)}" data-subject="${esc(item.subject)}">Reply</button>` : '';
}
function renderBrief(brief, cached = false) {
  spoken = brief.spoken_brief;
  savedAt = mailTime(brief.generated_at);
  $('#mail').textContent = brief.total_emails;
  $('#priority').textContent = brief.critical_count + brief.high_count;
  $('#replies').textContent = brief.reply_count;
  $('#status').textContent = `${cached ? 'Offline — saved' : brief.degraded ? 'Rules-based fallback — saved' : 'Saved'} brief from ${savedAt}`;
  $('#cards').innerHTML = brief.items.map(item => `<article class="card">
    <div class="badge">${esc(item.importance)} • ${esc(item.section)}</div>
    <div class="subject">${esc(item.subject)}</div><div class="sender">${esc(item.sender)}</div>
    <p class="summary">${esc(item.summary)}</p>
    ${item.requested_action ? `<div class="next">${esc(item.requested_action)}</div>` : ''}
    ${sourceLink(item)}${replyButton(item)}</article>`).join('') || '<p class="card">No priority mail needs attention.</p>';
}
async function load() {
  try { const brief = await request('/phone/briefing'); renderBrief(brief); localStorage.setItem('mail-brief', JSON.stringify(brief)); return brief; }
  catch (error) { const saved = localStorage.getItem('mail-brief'); if (saved) { const brief = JSON.parse(saved); renderBrief(brief, true); return brief; } else $('#status').textContent = error.message; }
}
function say(text, done, skipNative = false, keepOutputMuted = false) {
  if (currentUtterance) { currentUtterance.onend = currentUtterance.onerror = null; }
  stopSpeech();
  voice.mute(true);
  const finish = () => {
    currentUtterance = null;
    if (!keepOutputMuted) voice.mute(false);
    if (typeof done === 'function') done();
  };
  // Mobile speech engines frequently stop after a short utterance. Chain
  // small sentence-aware chunks so the complete answer is spoken.
  const chunks = [];
  for (const sentence of String(text).split(/(?<=[.!?])\s+/)) {
    if (!sentence) continue;
    if (sentence.length <= 240) chunks.push(sentence);
    else for (let i = 0; i < sentence.length; i += 220) chunks.push(sentence.slice(i, i + 220));
  }
  if (!chunks.length) chunks.push('');
  if (window.MorningMailSpeech && !skipNative) {
    const next = () => {
      if (!chunks.length) { finish(); return; }
      const id = String(Date.now()) + '-' + Math.random();
      const part = chunks.shift();
      nativeSpeech = {id, done: next, text: part};
      window.MorningMailSpeech.speak(part, id);
    };
    next(); return;
  }
  browserSpeechFallback = false;
  if (!window.speechSynthesis) { cancelReadback(); $('#voice-status').textContent = 'Speech playback is unavailable in this browser. Use typed chat or enable phone speech.'; return; }
  window.speechSynthesis.cancel();
  window.speechSynthesis.resume();
  const speakNext = () => {
    const utterance = new SpeechSynthesisUtterance(chunks.shift() || ''); utterance.lang = 'en-IN';
    utterance.volume = 1;
    currentUtterance = utterance;
    utterance.onend = () => chunks.length ? speakNext() : finish();
    utterance.onerror = () => { cancelReadback(); $('#voice-status').textContent = 'Playback stopped. Tap Listen to continue.'; };
    speechSynthesis.speak(utterance);
  };
  speakNext();
}
function cancelReadback() {
  readbackGeneration++; reviewTicket = '';
  if (currentUtterance) { currentUtterance.onend = currentUtterance.onerror = null; currentUtterance = null; }
  stopSpeech(); voice.mute(false);
}
function showResult(result) {
  if (window.showPlannerResult) window.showPlannerResult(result);
  cancelReadback();
  if (result.session_reset && result.session_key) {
    sessionKey = result.session_key;
    sessionStorage.setItem('mail-session', sessionKey);
    selectedReply = null; clearReplyMode(); draft = null; reviewTicket = '';
    voice.setSessionKey(sessionKey);
    voice.stop();
  }
  $('#priority-inbox').open = false;
  $('#answer').textContent = result.answer || 'Request interrupted.';
  $('#evidence').innerHTML = (result.evidence || []).map(item => `<div class="card"><b>${esc(item.subject)}</b><p>${esc(item.sender)}${item.received_at ? ' · ' + esc(mailTime(item.received_at)) : ''}</p>${item.retrieval_source ? `<small>Found in ${esc(item.retrieval_source.join(' and '))}</small>` : ''}${item.relevance_reason ? `<p><small>Why included: ${esc(item.relevance_reason)}</small></p>` : ''}${item.summary ? `<p>${esc(item.summary)}</p>` : ''}${sourceLink(item)}${replyButton(item)}</div>`).join('');
  draft = result.draft || null;
  $('#readback').hidden = !draft;
  $('#draft').textContent = draft ? `To: ${draft.to}\nSubject: ${draft.subject}\n\n${draft.body}` : '';
  $('#answer').scrollIntoView({behavior: 'smooth', block: 'start'});
}
$('#sync').addEventListener('click', async () => {
  $('#sync').disabled = true; $('#status').textContent = 'Checking for new mail…';
  try { await request('/phone/refresh', {}); const brief = await load(); if (brief && brief.degraded) say('Gemini was unavailable. I prepared a rules-based brief and marked it on screen.'); }
  catch (_) { await load(); const text = `I couldn't refresh your mail. ${savedAt ? 'Showing your saved brief from ' + savedAt : 'No saved briefing is available.'}`; $('#status').textContent = text; say(text); }
  finally { $('#sync').disabled = false; }
});
$('#listen').addEventListener('click', () => { cancelReadback(); voice.interrupt(); say(spoken || 'No briefing is ready yet.'); });
$('#recent').addEventListener('click', async () => {
  $('#recent').disabled = true; cancelReadback(); voice.interrupt();
  try {
    const result = await request('/phone/recent?limit=20');
    $('#answer').textContent = result.count ? `Showing ${result.count} recently synced emails.` : 'No email has been synced yet.';
    $('#evidence').innerHTML = result.emails.map(item => `<div class="card"><div class="badge">${esc(item.importance)}${item.degraded ? ' • RULES-BASED FALLBACK' : ''}</div><b>${esc(item.subject)}</b><p>${esc(item.sender)} · ${esc(mailTime(item.received_at))}</p><p>${esc(item.summary)}</p>${sourceLink(item)}${replyButton(item)}</div>`).join('');
  } catch (error) { $('#answer').textContent = error.message; }
  finally { $('#recent').disabled = false; }
});
$('#evidence').addEventListener('click', async event => {
  const contact = event.target.closest('[data-wa-index]');
  if (contact && whatsapp.matches) { selectWhatsAppContact(whatsapp.matches[Number(contact.dataset.waIndex)]); return; }
  const reply = event.target.closest('[data-reply]');
  if (reply) { await draftReply(reply); return; }
});
$('#cards').addEventListener('click', async event => {
  const reply = event.target.closest('[data-reply]');
  if (reply) await draftReply(reply);
});
async function draftReply(button) {
  cancelReadback(); voice.interrupt();
  selectedReply = {...button.dataset};
  $('#answer').textContent = `Replying to ${selectedReply.sender} about “${selectedReply.subject}”. What should the reply say?`;
  $('#voice-status').textContent = 'Type your message below, or tap Ask and speak it.';
  $('label[for="message"]').textContent = `Reply instructions for ${selectedReply.sender}`;
  $('#message').value = '';
  $('#message').placeholder = 'Example: Confirm the new meeting time.';
  $('#submit').textContent = 'Prepare reply draft';
  $('#question').scrollIntoView({behavior: 'smooth', block: 'center'});
  $('#message').focus({preventScroll: true});
}
function clearReplyMode() {
  selectedReply = null;
  $('label[for="message"]').textContent = 'Ask anything about your mail or dictate a reply';
  $('#message').placeholder = '';
  $('#submit').textContent = 'Ask assistant';
}
function withReplyContext(message) {
  if (!selectedReply) return {message};
  const payload = {message: `Reply to the selected email. My reply instructions: ${message}. Prepare a draft only. Do not send.`,
    reply_message_id: selectedReply.messageId, reply_thread_id: selectedReply.threadId};
  clearReplyMode(); return payload;
}
$('#commitments').addEventListener('click', async () => {
  $('#commitments').disabled = true; cancelReadback(); voice.interrupt();
  try {
    const result = await request('/phone/commitments');
    $('#answer').textContent = result.count ? `${result.you_owe} promises from you; ${result.they_owe} from others.` : 'No explicit promises are open.';
    $('#evidence').innerHTML = result.commitments.map(item => `<div class="card"><div class="badge">${item.direction === 'YOU_OWE' ? 'YOU PROMISED' : 'THEY PROMISED'}</div><b>${esc(item.subject)}</b><p>${esc(item.commitment)}</p>${item.due ? `<div class="next">${esc(item.due)}</div>` : ''}${sourceLink(item)}</div>`).join('');
    say(result.spoken_answer);
  } catch (error) { $('#answer').textContent = error.message; }
  finally { $('#commitments').disabled = false; }
});
$('#waiting').addEventListener('click', async () => {
  $('#waiting').disabled = true; cancelReadback(); voice.interrupt();
  try {
    const result = await request('/phone/waiting?days=3');
    $('#answer').textContent = result.count ? `${result.count} conversations have had no reply for at least three days.` : result.spoken_answer;
    $('#evidence').innerHTML = result.threads.map(item => `<div class="card"><div class="badge">WAITING</div><b>${esc(item.subject)}</b><p>Last sent ${esc(item.last_message_at || '')}</p><div class="next">Review · Draft follow-up · Defer</div>${sourceLink(item)}</div>`).join('');
    say(result.spoken_answer);
  } catch (error) { $('#answer').textContent = error.message; }
  finally { $('#waiting').disabled = false; }
});
$('#ask').addEventListener('click', () => {
  cancelReadback();
  if (window.MorningMailSpeech && window.MorningMailSpeech.listen) {
    voice.stop();
    $('#voice-status').textContent = 'Opening your phone assistant…';
    window.MorningMailSpeech.listen();
  } else voice.start();
});
window.mailVoiceTextResult = async (text, alternatives = []) => {
  const heard = String(text || '').trim();
  if (!heard) { $('#voice-status').textContent = 'I did not hear a question. Tap Ask your mail assistant and try again.'; return; }
  const payload = withReplyContext(heard);
  $('#transcript').textContent = heard; $('#voice-status').textContent = 'Checking your mail…';
  try {
    const result = await request('/phone/agent', {...payload, alternatives, input_mode: 'voice', session_key: sessionKey});
    showResult(result); if (!result.draft) say(result.spoken_answer);
  } catch (error) { $('#answer').textContent = error.message; $('#voice-status').textContent = 'Could not check your mail. You can try again or type your question.'; }
};
window.mailVoiceError = message => { $('#voice-status').textContent = message || 'Phone voice input is unavailable. Allow microphone access or type your question.'; };
$('#stop').addEventListener('click', () => { cancelReadback(); if (reconnectTimer) { clearTimeout(reconnectTimer); reconnectTimer = null; } voice.stop(); });
window.mailMicrophonePermission = granted => {
  if (!granted) $('#voice-status').textContent = 'Microphone permission was denied. Allow it in Android app permissions, or use typed chat.';
};
window.mailAppLifecycle = state => {
  if (state === 'background') {
    cancelReadback();
    if (reconnectTimer) { clearTimeout(reconnectTimer); reconnectTimer = null; }
    voice.stop();
    $('#voice-status').textContent = 'Voice paused while Morning Mail is in the background.';
  } else if (state === 'foreground') {
    $('#voice-status').textContent = 'Voice is off. Tap Ask to reconnect.';
  }
};
$('#question').addEventListener('submit', async event => {
  event.preventDefault(); const typed = $('#message').value.trim(); if (!typed) return;
  const plannerRequest = /\b(calendar|schedule|reschedule|remind|reminders?|appointment|am I free|my events|my meetings)\b|कैलेंडर|याद दिला|रिमाइंडर|मेरी बैठक|क्या मैं खाली|\b(?:yaad dila|main free|meri meeting)/i.test(typed) || (window.plannerActive && /move it|change it|cancel it|make it|what about|and tomorrow|उसे|उसको|इसे|\b(?:isko|usko)\b/i.test(typed));
  if (window.clearPlannerReview) window.clearPlannerReview();
  if (plannerRequest) whatsappMode(false);
  if (!plannerRequest && (/whats\s*app|व्हाट्स(?:ऐप|एप)/i.test(typed) || whatsapp.name)) {
    handleWhatsAppCommand(typed); $('#message').value = ''; return;
  }
  const payload = plannerRequest ? {message: typed} : withReplyContext(typed);
  $('#submit').disabled = true; const ticket = reviewTicket; cancelReadback(); voice.interrupt();
  try {
    const result = plannerRequest
      ? await request('/phone/planner/ask', {message: typed, session_key: sessionKey})
      : await request('/phone/agent', {...payload, input_mode: 'text', session_key: sessionKey, review_ticket: ticket});
    showResult(result); if (!result.draft) say(result.spoken_answer); $('#message').value = '';
  } catch (error) { $('#answer').textContent = error.message; }
  finally { $('#submit').disabled = false; }
});
$('#readback').addEventListener('click', async () => {
  if (!draft) return; cancelReadback(); voice.interrupt(); voice.mute(true);
  const generation = readbackGeneration;
  try {
    const review = await request(`/phone/drafts/${draft.id}/review`, {session_key: sessionKey});
    if (generation !== readbackGeneration) return;
    say(review.readback, async () => {
      if (generation !== readbackGeneration) return;
      try {
        await request(`/phone/drafts/${draft.id}/review-complete`, {session_key: sessionKey, review_ticket: review.review_ticket});
        if (generation !== readbackGeneration) return;
        reviewTicket = review.review_ticket;
        $('#voice-status').textContent = 'Read-back complete. Type done or send it within 60 seconds to send.';
        voice.mute(false);
      } catch (error) { cancelReadback(); $('#voice-status').textContent = error.message; }
    });
  } catch (error) { cancelReadback(); $('#voice-status').textContent = error.message; }
});
window.addEventListener('pagehide', () => { cancelReadback(); voice.stop(); });
document.addEventListener('visibilitychange', () => window.mailAppLifecycle(document.hidden ? 'background' : 'foreground'));
load();
