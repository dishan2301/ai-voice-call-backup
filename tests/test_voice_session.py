import asyncio

from app.voice.session import VoiceSession


def test_streaming_events_and_interruption_discard_late_mail_result():
    async def exercise():
        import threading
        started, release = threading.Event(), threading.Event()
        events = []

        class Agent:
            def run(self, text, session_key, **kwargs):
                kwargs["emit"]({"type": "tool.started", "tool": "search_mail"})
                started.set()
                release.wait(2)
                kwargs["emit"]({"type": "tool.completed", "tool": "search_mail"})
                return {"answer": "Two meeting mails. Newest or both?", "evidence": [{"message_id": "m1"}]}

        voice = VoiceSession(events.append, Agent)
        voice.start()
        voice.partial("Any meeting")
        task = asyncio.create_task(voice.respond("Any meeting mail?"))
        await asyncio.to_thread(started.wait, 2)
        voice.audio("fake-pcm")
        voice.interrupt()
        release.set()
        assert await task == {"cancelled": True}
        assert not any(e["type"] == "mail.result" for e in events)
        assert {"transcript.partial", "transcript.final", "tool.started", "response.audio.delta", "response.cancelled"} <= {e["type"] for e in events}
        voice.close()
        assert voice.state == "IDLE"
    asyncio.run(exercise())


def test_unknown_voice_confidence_never_authorizes_send():
    async def exercise():
        voice = VoiceSession(lambda _: None, lambda: (_ for _ in ()).throw(AssertionError("No send")))
        result = await voice.respond("done")
        assert result["state"] == "CONFIRMING"
    asyncio.run(exercise())


def test_live_voice_repairs_afro_before_mail_agent_search():
    async def exercise():
        seen = {}

        class Agent:
            def run(self, text, session_key, **kwargs):
                seen.update(text=text, session_key=session_key, confidence=kwargs["confidence_tier"])
                return {"answer": "Found mail.", "spoken_answer": "Found mail.", "evidence": []}

        events = []
        voice = VoiceSession(events.append, Agent, "voice-session-123456")
        await voice.respond("any mail afro zydus")
        assert seen["text"] == "any mail from zydus"
        assert seen["confidence"] == "UNKNOWN"
        assert any(event.get("text") == "any mail from zydus" for event in events)
    asyncio.run(exercise())


def test_closed_session_cannot_restart_or_call_mail_agent():
    async def exercise():
        events = []
        voice = VoiceSession(events.append, lambda: (_ for _ in ()).throw(AssertionError('closed')))
        voice.start()
        voice.close()
        count = len(events)
        voice.close()
        voice.audio('late')
        voice.interrupt()
        assert await voice.respond('late request') == {'cancelled': True}
        assert voice.state == 'IDLE'
        assert len(events) == count
    asyncio.run(exercise())


def test_m3_voice_reconnect_reuses_only_explicit_session_key():
    async def exercise():
        seen = []
        class Agent:
            def run(self, text, session_key, **kwargs):
                seen.append((text, session_key))
                return {"answer": text, "spoken_answer": text, "evidence": []}
        first = VoiceSession(lambda _: None, Agent, "reconnect-session-1234")
        interrupted = asyncio.create_task(first.respond("Find ABC"))
        first.interrupt()
        await interrupted
        first.close()
        second = VoiceSession(lambda _: None, Agent, "reconnect-session-1234")
        result = await second.respond("What did they say?")
        assert result["answer"] == "What did they say?"
        assert seen[-1] == ("What did they say?", "reconnect-session-1234")
        assert VoiceSession(lambda _: None, Agent).session_key != second.session_key
    asyncio.run(exercise())


def test_m4_final_transcript_event_is_distinct_from_display_only_interim_text():
    events = []
    voice = VoiceSession(events.append, lambda: None, "final-session-1234")
    voice.replace_transcript("Zydus ko reply")
    voice.final("Zydus ko reply bhejo")
    assert events == [
        {"type": "transcript.partial", "epoch": 0, "text": "Zydus ko reply"},
        {"type": "transcript.final", "epoch": 0, "text": "Zydus ko reply bhejo"},
    ]
