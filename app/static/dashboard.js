(() => {
  const synth = window.speechSynthesis;
  const Recognition = window.SpeechRecognition || window.webkitSpeechRecognition;
  const panel = document.querySelector('.assistant');
  const status = document.querySelector('#voice-status');
  const toggle = document.querySelector('#voice-toggle');
  const stop = document.querySelector('#voice-stop');
  const questionForm = document.querySelector('#mail-question');
  const questionInput = document.querySelector('#mail-question-text');
  const answer = document.querySelector('#mail-answer');
  let recognition;
  let assistantOn = false;
  let speaking = false;
  const sessionKey = sessionStorage.getItem('mail-session') || crypto.randomUUID();
  sessionStorage.setItem('mail-session', sessionKey);

  const setStatus = message => { status.textContent = message; };
  const resumeListening = () => {
    if (assistantOn && recognition) try { recognition.start(); } catch (_) {}
  };

  function say(text, label = 'Playing your mail brief…') {
    if (!synth) {
      setStatus('Browser voice is unavailable. Trying the computer voice…');
      return false;
    }
    speaking = true;
    setStatus(label);
    if (recognition && assistantOn) recognition.stop();
    synth.cancel();
    const utterance = new SpeechSynthesisUtterance(text);
    utterance.rate = Number(panel.dataset.voiceRate) / 165;
    utterance.volume = Number(panel.dataset.voiceVolume);
    utterance.onend = () => {
      speaking = false;
      setStatus(assistantOn ? 'Listening for “Hello Google”…' : 'Finished speaking.');
      resumeListening();
    };
    utterance.onerror = event => {
      speaking = false;
      setStatus(`Voice playback failed: ${event.error || 'unknown error'}.`);
      resumeListening();
    };
    synth.speak(utterance);
    return true;
  }

  async function speakMailBrief() {
    const current = document.querySelector('section[aria-labelledby="today-heading"] .listen-form');
    if (current) return say(JSON.parse(current.dataset.speech));
    say('There is no briefing yet. I am generating one now.', 'Generating your mail brief…');
    try {
      const response = await fetch('/briefings/generate', { method: 'POST' });
      const brief = await response.json();
      if (!response.ok) throw new Error(brief.detail || 'Brief generation failed');
      say(brief.spoken_brief);
    } catch (error) {
      setStatus(error.message || 'Could not generate the mail brief.');
    }
  }

  async function askGemini(message, inputMode = 'text') {
    setStatus('Checking your mail with Gemini…');
    try {
      const response = await fetch('/phone/agent', {method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({message, input_mode: inputMode, session_key: sessionKey})});
      const result = await response.json();
      if (!response.ok) throw new Error(result.detail || 'The mail assistant could not complete that request.');
      answer.textContent = [result.answer,
        result.draft ? `\nDraft for review only\nTo: ${result.draft.to}\nSubject: ${result.draft.subject}\n\n${result.draft.body}` : ''].filter(Boolean).join('\n');
      const evidence = document.querySelector('#mail-evidence'); evidence.replaceChildren();
      for (const item of result.evidence || []) {
        const card = document.createElement('p');
        const origin = document.createElement('small');
        const reason = document.createElement('small');
        const link = document.createElement('a');
        origin.textContent = item.retrieval_source ? `Found in ${item.retrieval_source.join(' and ')} · ` : '';
        reason.textContent = item.relevance_reason ? ` · Why included: ${item.relevance_reason}` : '';
        link.textContent = `Source: ${item.subject || 'Email'} — ${item.sender || 'Unknown sender'}`;
        if (item.message_id) { link.href = `https://mail.google.com/mail/u/0/#all/${encodeURIComponent(item.message_id)}`; link.target = '_blank'; link.rel = 'noopener noreferrer'; }
        card.append(origin, link, reason); evidence.append(card);
      }
      if (inputMode === 'voice' && result.spoken_answer) say(result.spoken_answer, 'Gemini answered from your mail.');
      else setStatus('Gemini reply ready. Check the source emails before acting.');
    } catch (error) { answer.textContent = error.message; setStatus('Could not verify an answer. Your saved briefing is still available.'); }
  }

  questionForm.addEventListener('submit', event => {
    event.preventDefault();
    const message = questionInput.value.trim();
    if (message) askGemini(message);
  });

  document.querySelectorAll('.listen-form').forEach(form => form.addEventListener('submit', event => {
    if (synth) {
      event.preventDefault();
      say(JSON.parse(form.dataset.speech));
    } else {
      setStatus('Browser voice is unavailable. Trying the computer voice…');
    }
  }));

  document.querySelectorAll('.stop-form').forEach(form => form.addEventListener('submit', event => {
    if (synth) {
      event.preventDefault();
      synth.cancel();
      speaking = false;
      setStatus(assistantOn ? 'Listening for “Hello Google”…' : 'Voice stopped.');
      resumeListening();
    }
  }));

  stop.addEventListener('click', () => {
    assistantOn = false;
    if (recognition) recognition.stop();
    if (synth) synth.cancel();
    toggle.textContent = '🎙 Enable voice assistant';
    setStatus('Voice assistant is off.');
  });

  if (!Recognition) {
    toggle.disabled = true;
    setStatus('Voice commands are not supported in this browser. Use Chrome or Edge, or press Listen.');
    return;
  }

  recognition = new Recognition();
  recognition.continuous = true;
  recognition.interimResults = false;
  recognition.lang = 'en-IN';
  recognition.onstart = () => setStatus('Listening for “Hello Google”…');
  recognition.onresult = event => {
    const phrase = event.results[event.results.length - 1][0].transcript.trim();
    setStatus(`Heard: “${phrase}”`);
    const command = phrase.replace(/^(hello|hey) google[, ]*/i, '').trim();
    if (/^(hello|hey) google\b/i.test(phrase)) {
      if (/(mail|email).*(brief|summary)|brief.*(mail|email)/i.test(command)) speakMailBrief();
      else if (command) askGemini(command, 'voice');
      else setStatus('Listening. Ask a question about your mail.');
    }
  };
  recognition.onerror = event => {
    if (event.error !== 'no-speech') setStatus(`Microphone error: ${event.error}. Check browser permission.`);
  };
  recognition.onend = () => { if (!speaking) resumeListening(); };
  toggle.addEventListener('click', () => {
    assistantOn = !assistantOn;
    toggle.textContent = assistantOn ? '● Voice assistant on' : '🎙 Enable voice assistant';
    if (assistantOn) resumeListening();
    else {
      recognition.stop();
      setStatus('Voice assistant is off.');
    }
  });
})();
