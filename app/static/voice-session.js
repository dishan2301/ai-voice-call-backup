/* Provider credentials stay on the backend. */
class MailVoiceSession {
  constructor(apiUrl, sessionKey, receive) {
    Object.assign(this, {apiUrl, sessionKey, receive, generation: 0, state: 'IDLE', disconnectTimer: null, outputMuted: false});
  }
  setSessionKey(sessionKey) { this.sessionKey = sessionKey; }
  update(state) { this.state = state; this.receive({type: 'session.state', state}); }
  async start() {
    if (this.state !== 'IDLE') return;
    const generation = ++this.generation;
    this.update('CONNECTING');
    try {
      if (!window.isSecureContext || !navigator.mediaDevices) throw Error('Microphone needs HTTPS or localhost. Use typed chat below.');
      let stream;
      try {
        stream = await navigator.mediaDevices.getUserMedia({audio: {echoCancellation: true, noiseSuppression: true}});
      } catch (firstError) {
        // Some Android WebViews cannot open the microphone with processing constraints.
        // Retry with the device's default capture settings before giving up.
        if (!['NotReadableError', 'OverconstrainedError', 'AbortError'].includes(firstError.name)) throw firstError;
        stream = await navigator.mediaDevices.getUserMedia({audio: true});
      }
      if (generation !== this.generation) { stream.getTracks().forEach(t => t.stop()); return; }
      this.stream = stream;
      let rtcConfig = {iceServers: []};
      try {
        const configResponse = await fetch(this.apiUrl('/phone/voice/config'));
        if (configResponse.ok) rtcConfig = await configResponse.json();
      } catch (_) {}
      this.pc = new RTCPeerConnection({iceServers: rtcConfig.iceServers || []});
      this.audio = new Audio();
      this.audio.autoplay = !this.outputMuted;
      this.audio.playsInline = true;
      this.audio.volume = 1.0;
      this.audio.muted = this.outputMuted;
      this.audio.onvolumechange = () => {
        if (this.audio && !this.audio.muted && this.audio.volume < 1) this.audio.volume = 1.0;
      };
      this.pc.ontrack = event => {
        this.audio.srcObject = new MediaStream([event.track]);
        // Some mobile browsers retain a ducked/attenuated volume after
        // microphone capture starts. Restore the app output level per track.
        this.audio.volume = 1.0;
        this.audio.muted = this.outputMuted;
        if (!this.outputMuted) this.playAudio();
      };
      this.channel = this.pc.createDataChannel('mail-events');
      this.channel.onopen = () => this.update('LISTENING');
      this.channel.onmessage = ({data}) => {
        if (generation === this.generation) { try { this.receive(JSON.parse(data)); } catch (_) {} }
      };
      this.pc.onconnectionstatechange = () => {
        if (!this.pc) return;
        if (this.pc.connectionState === 'connected') clearTimeout(this.disconnectTimer);
        if (this.pc.connectionState === 'failed') {
          this.stop(); this.receive({type: 'session.error', message: 'Voice disconnected. Reconnect or type your request.'});
        } else if (this.pc.connectionState === 'disconnected') {
          // Mobile WebRTC briefly enters this state while network candidates settle.
          clearTimeout(this.disconnectTimer);
          this.disconnectTimer = setTimeout(() => {
            if (this.pc && this.pc.connectionState === 'disconnected') {
              this.stop(); this.receive({type: 'session.error', message: 'Voice disconnected. Reconnect or type your request.'});
            }
          }, 5000);
        }
      };
      stream.getTracks().forEach(track => this.pc.addTrack(track, stream));
      await this.pc.setLocalDescription(await this.pc.createOffer());
      await new Promise((resolve, reject) => {
        if (this.pc.iceGatheringState === 'complete') { resolve(); return; }
        const timeout = setTimeout(() => reject(Error('Voice connection timed out.')), 10000);
        this.pc.addEventListener('icegatheringstatechange', () => {
          if (this.pc && this.pc.iceGatheringState === 'complete') { clearTimeout(timeout); resolve(); }
        });
      });
      const response = await fetch(this.apiUrl('/phone/voice/offer'), {method: 'POST',
        headers: {'Content-Type': 'application/json'}, body: JSON.stringify({sdp: this.pc.localDescription.sdp, type: 'offer', session_key: this.sessionKey})});
      const answer = await response.json();
      if (!response.ok) throw Error(answer.detail || 'Voice could not connect.');
      if (generation !== this.generation) {
        fetch(this.apiUrl('/phone/voice/' + answer.session_id), {method: 'DELETE'}).catch(() => {}); return;
      }
      this.peerId = answer.session_id;
      await this.pc.setRemoteDescription({sdp: answer.sdp, type: answer.type});
    } catch (error) {
      this.stop();
      const message = error.name === 'NotReadableError'
        ? 'The microphone could not start. Close other apps using it, check Morning Mail microphone permission, or use the typed question box.'
        : error.name === 'NotAllowedError' || /permission denied/i.test(error.message)
          ? 'Microphone permission was denied. Allow it in Android app permissions, or use the typed question box.'
          : error.message;
      this.receive({type: 'session.error', message});
    }
  }
  interrupt() {
    if (window.speechSynthesis) window.speechSynthesis.cancel();
    if (window.MorningMailSpeech) window.MorningMailSpeech.stop();
    if (this.channel && this.channel.readyState === 'open') this.channel.send(JSON.stringify({type: 'interrupt'}));
  }
  contactMode(active) {
    if (this.channel && this.channel.readyState === 'open') this.channel.send(JSON.stringify({type: 'contact.mode', active: !!active}));
  }
  playBriefing(runId) {
    if (this.channel && this.channel.readyState === 'open') {
      this.channel.send(JSON.stringify({type: 'briefing.play', run_id: runId})); return true;
    }
    return false;
  }
  mute(value) {
    // Keep capture alive during exact read-back so the user can interrupt it.
    // Pause the competing output for the entire local speech, including chunk gaps.
    const wasMuted = this.outputMuted;
    this.outputMuted = value;
    if (this.audio) {
      this.audio.autoplay = !this.outputMuted;
      this.audio.muted = this.outputMuted;
      if (this.outputMuted) this.audio.pause();
      else if (wasMuted && this.audio.srcObject) this.playAudio();
    }
  }
  playAudio() {
    const audio = this.audio;
    if (!audio || this.outputMuted) return;
    audio.play().catch(() => {
      if (this.audio === audio && !this.outputMuted) {
        this.receive({type: 'session.error', message: 'Audio playback was blocked. Restart voice.'});
      }
    });
  }
  stop() {
    ++this.generation;
    clearTimeout(this.disconnectTimer); this.disconnectTimer = null;
    if (this.stream) this.stream.getTracks().forEach(track => track.stop());
    if (this.pc) { this.pc.onconnectionstatechange = null; this.pc.close(); }
    if (this.audio) { this.audio.pause(); this.audio.srcObject = null; }
    if (this.peerId) fetch(this.apiUrl('/phone/voice/' + this.peerId), {method: 'DELETE', keepalive: true}).catch(() => {});
    this.pc = this.stream = this.audio = this.peerId = null;
    this.update('IDLE');
  }
}
window.MailVoiceSession = MailVoiceSession;
