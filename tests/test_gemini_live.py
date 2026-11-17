import asyncio
import json
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from app.voice.gemini_live import GeminiLive


def test_follow_up_waits_for_late_final_transcript_and_keeps_all_segments():
    async def exercise():
        queue = asyncio.Queue()
        answered = asyncio.Event()
        requests = []

        class Voice:
            epoch = 0
            closed = False
            transcript = ""

            def interrupt(self):
                self.epoch += 1
                self.transcript = ""

            def replace_transcript(self, text):
                self.transcript = text

            def transition(self, state):
                pass

            def event(self, kind, **fields):
                pass

            async def respond(self, text):
                requests.append(text)
                answered.set()
                return {"answer": "Found it", "spoken_answer": ""}

        class Socket:
            def __aiter__(self):
                return self

            async def __anext__(self):
                return await queue.get()

            async def send(self, raw):
                if "toolResponse" in raw:
                    live.turn_complete.set()

        voice = Voice()
        live = GeminiLive(None, voice, lambda _: None, lambda: None)
        live.socket = Socket()
        task = asyncio.create_task(live.receive())
        try:
            for index, segments in enumerate((
                ("Tell me more", "about that mail"),
                ("Who sent it", "and when"),
                ("What should", "I reply"),
            )):
                answered.clear()
                await queue.put(json.dumps({"toolCall": {"functionCalls": [{"id": str(index), "name": "mail_request"}]}}))
                await asyncio.sleep(0.03)
                for segment in segments:
                    await queue.put(json.dumps({"serverContent": {"inputTranscription": {"text": segment}}}))
                await asyncio.wait_for(answered.wait(), 1)
                assert requests[-1] == " ".join(segments)
                assert voice.transcript == ""
            assert len(requests) == 3
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(exercise())


def test_mail_tool_result_is_sent_back_to_gemini_for_audio_generation():
    async def exercise():
        sent = []

        class Voice:
            epoch = 0
            closed = False

            async def respond(self, text):
                assert text == "Find the device email"
                return {"answer": "Found it", "spoken_answer": "I found the device email from Priya.",
                        "evidence": [{"message_id": "device", "received_at": datetime(2026, 9, 23, 7, 4, tzinfo=timezone.utc)}]}

        class Socket:
            async def send(self, raw):
                sent.append(json.loads(raw))
                if "toolResponse" in sent[-1]:
                    live.turn_complete.set()
                elif "clientContent" in sent[-1]:
                    live.audio_chunks_received += 1
                    live.turn_complete.set()

        live = GeminiLive(None, Voice(), lambda _: None, lambda: None)
        live.socket = Socket()
        await live.delegate({"id": "call-1", "name": "mail_request"}, "Find the device email")

        assert sent[0]["toolResponse"]["functionResponses"][0]["id"] == "call-1"
        assert sent[0]["toolResponse"]["functionResponses"][0]["response"]["answer"] == "Found it"
        assert sent[0]["toolResponse"]["functionResponses"][0]["response"]["evidence"][0]["received_at"].endswith('+00:00')
        prompt = sent[1]["clientContent"]["turns"][0]["parts"][0]["text"]
        assert "Gemini voice" in prompt
        assert "I found the device email from Priya." in prompt

    asyncio.run(exercise())


def test_mail_agent_exception_reports_mail_error_without_crashing_reporter():
    async def exercise():
        events = []

        class Voice:
            epoch = 0
            closed = False

            async def respond(self, text):
                raise RuntimeError("mail agent unavailable")
            def event(self, kind, **fields):
                events.append((kind, fields))

        class Socket:
            async def send(self, raw):
                if "toolResponse" in raw:
                    live.turn_complete.set()

        live = GeminiLive(None, Voice(), lambda _: None, lambda: None)
        live.socket = Socket()
        await live.delegate({"id": "call-1", "name": "mail_request"}, "Find the device email")

        assert events
        assert events[-1][0] == "session.error"
        assert "could not finish" in events[-1][1]["message"]

    asyncio.run(exercise())


def test_m45_whatsapp_request_stays_out_of_mail_agent_and_contact_book():
    async def exercise():
        events, sent = [], []
        class Voice:
            epoch = 0
            closed = False
            contact_mode = False
            async def respond(self, text):
                raise AssertionError("WhatsApp request reached MailAgent")
            def event(self, kind, **fields):
                events.append((kind, fields))
        class Socket:
            async def send(self, raw):
                sent.append(json.loads(raw))
                live.turn_complete.set()
        live = GeminiLive(None, Voice(), lambda _: None, lambda: None)
        live.socket = Socket()
        await live.delegate({"id": "wa-1", "name": "mail_request"},
                            "Send a WhatsApp message to Priya saying hello")
        assert events[0] == ("contact.request", {
            "text": "Send a WhatsApp message to Priya saying hello"})
        response = sent[0]["toolResponse"]["functionResponses"][0]["response"]
        assert "contact" in response["spoken_answer"].lower()
        assert "contacts" not in json.dumps(response).lower()
    asyncio.run(exercise())


@pytest.mark.parametrize("completion", ["silent", "tool_timeout", "speech_timeout", "send_error"])
def test_answer_falls_back_even_if_acknowledgement_audio_played_during_search(completion):
    async def exercise():
        events = []

        class Voice:
            epoch = 0
            closed = False

            async def respond(self, text):
                live.audio_chunks_received += 1  # "Let me check" is not the answer.
                return {"answer": "Found it", "spoken_answer": "I found the device email."}
            def event(self, kind, **fields):
                events.append((kind, fields))

        class Socket:
            async def send(self, raw):
                if completion == "send_error":
                    raise RuntimeError("Disconnected")
                if "toolResponse" in raw and completion != "tool_timeout":
                    live.turn_complete.set()
                elif "clientContent" in raw and completion != "speech_timeout":
                    live.turn_complete.set()

        live = GeminiLive(None, Voice(), lambda _: None, lambda: None)
        live.socket = Socket()
        with patch("app.voice.gemini_live.TURN_TIMEOUT_SECONDS", 0.01):
            await live.delegate({"id": "call-1", "name": "mail_request"}, "Find the device email")

        assert events[-1] == ("speech.fallback", {"text": "I found the device email."})
        assert live.allow_audio is False  # Late provider chunks must not duplicate TTS.

    asyncio.run(exercise())


@pytest.mark.parametrize("interrupt_at", ["retrieval", "toolResponse", "clientContent"])
def test_interrupted_answer_cannot_start_late_speech(interrupt_at):
    async def exercise():
        events, sent = [], []

        class Voice:
            epoch = 0
            closed = False

            async def respond(self, text):
                if interrupt_at == "retrieval":
                    self.epoch += 1
                    return {"cancelled": True}
                return {"spoken_answer": "The old meeting answer."}

            def event(self, kind, **fields):
                events.append((kind, fields))

        voice = Voice()
        live = GeminiLive(None, voice, lambda _: None, lambda: None)

        class Socket:
            async def send(self, raw):
                sent.append(json.loads(raw))
                if interrupt_at in sent[-1]:
                    voice.epoch += 1
                live.turn_complete.set()

        live.socket = Socket()
        await live.delegate({"id": "old-call", "name": "mail_request"}, "Read the meeting mail")
        assert events == []
        assert len(sent) == {"retrieval": 0, "toolResponse": 1, "clientContent": 2}[interrupt_at]

    asyncio.run(exercise())
