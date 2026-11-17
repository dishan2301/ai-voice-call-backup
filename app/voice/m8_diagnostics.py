"""Opt-in synthetic-test metadata; never retain transcripts or audio."""
import struct
import time


class Diagnostics:
    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.started = clock()
        self.events = {}
        self.counts = {}
        self.first_audio = None
        self.first_socket = None
        self.first_rtp = None
        self.speech_end = None
        self.peak = 0

    def event(self, name):
        self.counts[name] = self.counts.get(name, 0) + 1
        self.events.setdefault(name, self.clock() - self.started)

    def provider(self, event):
        content = event.get('serverContent', {})
        for field in ('inputTranscription', 'interimInputTranscription', 'outputTranscription',
                      'turnComplete', 'generationComplete', 'interrupted', 'activityStart', 'activityEnd'):
            if content.get(field):
                self.event(field)
        if content.get('inputTranscription', {}).get('finished'):
            self.event('input_transcription_finished')
        parts = content.get('modelTurn', {}).get('parts', [])
        if parts:
            self.event('response_event')
        # Absence of activity metadata must remain unknown, never inferred as silence.
        if content.get('activityEnd'):
            self.speech_end = self.clock()

    def audio(self, pcm):
        if self.first_audio is None:
            self.first_audio = self.clock()
        self.event('audio_chunks')
        self.counts['audio_bytes'] = self.counts.get('audio_bytes', 0) + len(pcm)
        samples = [v[0] for v in struct.iter_unpack('<h', pcm)]
        self.counts['nonzero_samples'] = self.counts.get('nonzero_samples', 0) + sum(v != 0 for v in samples)
        self.peak = max(self.peak, max((abs(v) for v in samples), default=0))

    def socket_frame(self):
        if self.first_socket is None:
            self.first_socket = self.clock()
        self.event('audiosocket_frames')

    def rtp_observed(self):
        """Call only from an actual packet/control event observer."""
        if self.first_rtp is None:
            self.first_rtp = self.clock()

    def snapshot(self):
        relative = lambda value: None if value is None else value-self.started
        return {'counts': dict(self.counts), 'first_events_seconds': dict(self.events),
                'peak_amplitude': self.peak, 'first_audio_seconds': relative(self.first_audio),
                'first_socket_seconds': relative(self.first_socket), 'first_rtp_seconds': relative(self.first_rtp),
                'speech_end_to_audio_seconds': None if self.speech_end is None or self.first_audio is None
                else self.first_audio-self.speech_end, 'elapsed_seconds': self.clock()-self.started}
