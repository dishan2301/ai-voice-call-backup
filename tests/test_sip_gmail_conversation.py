import asyncio
import base64
import threading
from unittest.mock import AsyncMock, Mock

import pytest

from app.email.gmail import GmailProvider
from app.voice.gmail_conversation import GmailCallTools, GmailConversationLive


class FakeGmail:
    def __init__(self):
        self.searches = []
        self.reads = []
        self.hold = None
        self.started = threading.Event()
        self.fail = False

    def search_metadata(self, query, limit, *, inbox):
        self.searches.append((query, limit, inbox))
        self.started.set()
        if self.hold:
            assert self.hold.wait(2)
        if self.fail:
            raise OSError("private exception")
        rows = [{"id": query or "latest", "sender": "Asha", "subject": query}]
        if query == "ambiguous":
            rows.append({"id": "second", "sender": "Ravi", "subject": query})
        return {"messages": rows, "complete": True}

    def get_message(self, message_id):
        self.reads.append(message_id)
        return {"message_id": message_id, "body": "Meeting at three."}


def test_metadata_batch_preserves_order_and_excludes_bodies():
    service = Mock()
    messages = service.users.return_value.messages.return_value
    messages.list.return_value.execute.return_value = {
        "messages": [{"id": "a"}, {"id": "b"}], "nextPageToken": "more"}
    batch = service.new_batch_http_request.return_value

    def respond():
        callback = service.new_batch_http_request.call_args.kwargs["callback"]
        for mid in ["b", "a"]:
            callback(mid, {"id": mid, "internalDate": "1780000000000",
                           "snippet": "Preview", "labelIds": ["UNREAD"]}, None)

    batch.execute.side_effect = respond
    output = GmailProvider(service).search_metadata("", 2, inbox=True)
    assert [row["id"] for row in output["messages"]] == ["a", "b"]
    assert output["complete"] is False
    assert messages.list.call_args.kwargs["labelIds"] == ["INBOX"]
    assert all(call.kwargs["format"] == "metadata" for call in messages.get.call_args_list)
    assert not any("body" in row for row in output["messages"])


def test_warm_cache_expiry_and_call_scoped_body_access():
    async def exercise():
        provider = FakeGmail()
        now = [0.]
        tools = GmailCallTools(lambda: provider, clock=lambda: now[0])
        try:
            await tools.search(max_results=20, refresh=True, publish=False)
            with pytest.raises(ValueError):
                await tools.read("latest")
            assert (await tools.search())["cached"]
            assert len(provider.searches) == 1 and not provider.reads
            assert (await tools.read("latest"))["message"]["body"] == "Meeting at three."
            assert (await tools.read("latest"))["cached"]
            assert provider.reads == ["latest"]
            now[0] += 61
            assert not (await tools.search())["cached"]
            assert len(provider.searches) == 2
        finally:
            tools.clear()
        assert not tools.references and not tools.bodies

    asyncio.run(exercise())


def function(event_id, name, args):
    return {"toolCall": {"functionCalls": [{"id": event_id, "name": name, "args": args}]}}


def audio():
    return {"inlineData": {"mimeType": "audio/pcm;rate=24000",
                           "data": base64.b64encode(bytes(960)).decode()}}


async def complete_jobs(live):
    await asyncio.gather(*list(live.jobs))


def test_continuous_session_preserves_topic_after_many_follow_ups():
    async def exercise():
        provider = FakeGmail()
        voice, output = Mock(), Mock()
        live = GmailConversationLive(None, voice, output, Mock(), lambda: provider)
        live.send = AsyncMock()
        try:
            await live.handle_event(function("search", "search_mail", {"query": "meeting"}))
            await complete_jobs(live)
            await live.handle_event({"serverContent": {"turnComplete": True}})
            await live.handle_event(function("read", "read_mail", {"id": "meeting"}))
            await complete_jobs(live)
            await live.handle_event({"serverContent": {"turnComplete": True}})
            for i in range(25):
                await live.handle_event({"serverContent": {
                    "inputTranscription": {"text": "Who sent it?" if i == 0 else "Tell me about it."},
                    "outputTranscription": {"text": "Asha sent it."},
                    "modelTurn": {"parts": [audio()]}, "turnComplete": True}})
            assert output.call_count == 25
            assert len(live.turns) == 20 and live.topic["id"] == "meeting"
            assert provider.searches == [("meeting", 5, False)]
            assert provider.reads == ["meeting"]
            assert live.send.await_count == 2
            await live.handle_event(function("multiple", "search_mail", {"query": "ambiguous"}))
            await complete_jobs(live)
            assert len(live.send.await_args.args[0]["toolResponse"]["functionResponses"][0]["response"]["messages"]) == 2
            await live.handle_event({"serverContent": {"outputTranscription": {
                "text": "Which one do you mean, Asha's or Ravi's?"}, "turnComplete": True}})
            assert live.turns[-1][1].endswith("?") and provider.reads == ["meeting"]
            await live.handle_event(function("new", "search_mail", {"query": "invoice"}))
            await complete_jobs(live)
            await live.handle_event({"serverContent": {"turnComplete": True}})
            assert live.topic["id"] == "invoice"
        finally:
            await live.close()
        assert not live.turns and live.topic is None

    asyncio.run(exercise())


def test_interruptions_discard_audio_and_late_results_then_allow_new_turn():
    async def exercise():
        provider = FakeGmail()
        provider.hold = threading.Event()
        voice, output, clear = Mock(), Mock(), Mock()
        live = GmailConversationLive(None, voice, output, clear, lambda: provider)
        live.send = AsyncMock()
        try:
            await live.handle_event(function("old", "search_mail", {"query": "meeting"}))
            assert await asyncio.to_thread(provider.started.wait, 1)
            # Partial transcript alone does not cancel an in-flight tool.
            await live.handle_event({"serverContent": {"inputTranscription": {"text": "wait"}}})
            assert live.jobs and not clear.called
            await live.handle_event({"serverContent": {"interrupted": True, "modelTurn": {"parts": [audio()]}}})
            await live.handle_event({"serverContent": {"modelTurn": {"parts": [audio()]}}})
            assert clear.called and not output.called
            provider.hold.set()
            await asyncio.gather(*list(live.jobs), return_exceptions=True)
            live.send.assert_not_awaited()
            await live.handle_event({"serverContent": {"turnComplete": True}})
            await live.handle_event(function("new", "search_mail", {"query": "invoice"}))
            await complete_jobs(live)
            assert live.send.await_args.args[0]["toolResponse"]["functionResponses"][0]["id"] == "new"
            await live.handle_event({"serverContent": {"modelTurn": {"parts": [audio()]}}})
            assert output.call_count == 1
            await live.handle_event({"serverContent": {"interrupted": True}})
            assert clear.call_count == 2
        finally:
            provider.hold.set()
            await live.close()

    asyncio.run(exercise())


def test_tool_error_is_safe_and_reply_can_continue():
    async def exercise():
        provider = FakeGmail()
        provider.fail = True
        live = GmailConversationLive(None, Mock(), Mock(), Mock(), lambda: provider)
        live.send = AsyncMock()
        try:
            await live.handle_event(function("error", "search_mail", {}))
            await complete_jobs(live)
            assert "Please try again" in str(live.send.await_args)
            assert "private exception" not in str(live.send.await_args)
        finally:
            await live.close()

    asyncio.run(exercise())


def test_reconnect_keeps_completed_turns_and_reference(monkeypatch):
    from app.voice.gemini_live import GeminiLive

    class Socket:
        def __init__(self, events):
            self.events = events
            self.close = AsyncMock()

        async def __aiter__(self):
            import json
            for event in self.events:
                yield json.dumps(event)

    async def exercise():
        provider = FakeGmail()
        live = GmailConversationLive(None, Mock(), Mock(), Mock(), lambda: provider)
        live.socket = Socket([{"sessionResumptionUpdate": {"resumable": True, "newHandle": "test"}},
                              {"goAway": {"timeLeft": "2s"}}])
        live.turns.append(("question", "answer", ()))
        live.topic = {"id": "meeting"}
        resumed = []

        async def reopen(session):
            resumed.append(session.resume_handle)
            session.socket = Socket([{"serverContent": {
                "inputTranscription": {"text": "Who sent it?"},
                "outputTranscription": {"text": "Asha sent it."}, "turnComplete": True}}])

        monkeypatch.setattr(GeminiLive, "start", reopen)
        try:
            with pytest.raises(RuntimeError, match="connection ended"):
                await live.receive()
            assert resumed == ["test"]
            assert len(live.turns) == 2 and live.topic["id"] == "meeting"
            assert not provider.searches
        finally:
            await live.close()

    asyncio.run(exercise())
