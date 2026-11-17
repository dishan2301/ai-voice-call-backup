"""Opt-in live provider smoke test using synthetic mail, never real Gmail or sending.

This exercises text-triggered Live tool delegation and generated audio, not ASR.
"""
import asyncio
from array import array
import sys
from pathlib import Path
from datetime import datetime, timezone
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import get_settings
from app.voice.session import VoiceSession
from app.voice.gemini_live import GeminiLive


async def main():
    audio_ready = asyncio.Event()
    delegated = asyncio.Event()
    finished = asyncio.Event()
    requests = []
    peak = 0
    samples_squared = 0
    sample_count = 0
    class SyntheticAgent:
        def run(self, message, session_key, **kwargs):
            requests.append((message, session_key))
            answer = ("Two meeting mails. Read the newest, or both?" if len(requests) == 1
                      else "The newest meeting email asks you to confirm attendance on Friday.")
            return {"answer": answer,
                    "spoken_answer": answer,
                    "evidence": [{"message_id": "synthetic-1", "thread_id": "synthetic-thread",
                                  "received_at": datetime(2026, 9, 23, 7, 4, tzinfo=timezone.utc)}],
                    "state": "NEEDS_CLARIFICATION" if len(requests) == 1 else "ANSWERING"}
    def event(value):
        if value['type'] == 'mail.result': delegated.set()
        if value.get('state') == 'LISTENING' and audio_ready.is_set(): finished.set()
    def audio(data):
        nonlocal peak, samples_squared, sample_count
        if data and delegated.is_set(): audio_ready.set()
        if data and delegated.is_set():
            samples = array('h')
            samples.frombytes(data)
            peak = max(peak, max(abs(sample) for sample in samples))
            samples_squared += sum(sample * sample for sample in samples)
            sample_count += len(samples)
    voice = VoiceSession(event, SyntheticAgent)
    live = GeminiLive(get_settings(), voice, audio, lambda: None)
    task = None
    try:
        await live.start()
        task = asyncio.create_task(live.receive())
        turns = ['Do I have meeting mail?', 'Read the newest one.']
        for text in turns:
            delegated.clear()
            audio_ready.clear()
            finished.clear()
            voice.partial(text)
            live.final_transcript = text  # Synthetic finalized input; this check does not exercise ASR.
            await live.send({'clientContent': {'turns': [{'role':'user', 'parts':[{'text':text}]}], 'turnComplete':True}})
            await asyncio.wait_for(delegated.wait(), 45)
            await asyncio.wait_for(audio_ready.wait(), 15)
            await asyncio.wait_for(finished.wait(), 15)
        assert [message for message, _ in requests] == turns
        assert len({key for _, key in requests}) == 1
        print('Two live turns verified: clarification, follow-up delegation, audio, and return to listening. Synthetic mail; text input, not ASR.')
        if sample_count:
            print(f'Live PCM level: peak {peak / 32768:.3f}, RMS {(samples_squared / sample_count) ** 0.5 / 32768:.3f}')
    finally:
        voice.close()
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await live.close()


if __name__ == '__main__':
    try: asyncio.run(main())
    except Exception as exc:
        print('Live smoke test failed:', type(exc).__name__)
        raise SystemExit(1)
