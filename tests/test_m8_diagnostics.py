import struct
from app.voice.m8_diagnostics import Diagnostics


def test_synthetic_counts_timing_and_privacy():
    now = [10.0]
    d = Diagnostics(lambda: now[0])
    d.provider({'serverContent': {'inputTranscription': {'text': 'SECRET FIXTURE', 'finished': True}, 'activityEnd': True}})
    now[0] = 10.5
    d.audio(struct.pack('<hhh', 0, -32768, 100))
    now[0] = 10.6
    d.socket_frame()
    now[0] = 10.7
    d.rtp_observed()
    d.provider({'serverContent': {'turnComplete': True, 'generationComplete': True}})
    result = d.snapshot()
    assert result['counts']['audio_bytes'] == 6
    assert result['counts']['nonzero_samples'] == 2
    assert result['peak_amplitude'] == 32768
    assert result['speech_end_to_audio_seconds'] == .5
    assert result['counts']['audiosocket_frames'] == 1
    assert 'SECRET' not in str(result)


def test_missing_measurements_stay_unknown():
    result = Diagnostics().snapshot()
    assert result['first_rtp_seconds'] is None
    assert result['speech_end_to_audio_seconds'] is None
