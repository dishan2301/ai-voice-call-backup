import asyncio
from datetime import datetime, timezone
import time
from unittest.mock import AsyncMock, Mock

import pytest

from app.email.gmail import GmailProvider
from app.voice.gmail_conversation import GmailCallTools, GmailConversationLive
from app.voice.gmail_ist import date_window, gmail_filters

BEFORE = datetime(2026, 11, 18, 18, 29, 59, tzinfo=timezone.utc)
AFTER = datetime(2026, 11, 18, 18, 30, tzinfo=timezone.utc)


@pytest.mark.parametrize("now,label,days", [
    (BEFORE, "today", ("2026-11-18", "2026-11-19")),
    (AFTER, "today", ("2026-11-19", "2026-11-20")),
    (AFTER, "yesterday", ("2026-11-18", "2026-11-19")),
    (AFTER, "last_7_days", ("2026-11-13", "2026-11-20")),
    (AFTER, "this_week", ("2026-11-16", "2026-11-20")),
    (AFTER, "last_month", ("2026-10-01", "2026-11-01")),
])
def test_calendar_ranges(now, label, days):
    start, end = date_window(now, label)
    assert (start.date().isoformat(), end.date().isoformat()) == days
    assert start.utcoffset().total_seconds() == 19800
    assert end.hour == 0
    query, _ = gmail_filters("", now=now, scope="received", filters={"range": label})
    assert f"after:{int(start.timestamp())} before:{int(end.timestamp()) - 1}" in query


def test_custom_dates_and_scope():
    args = {"range": "custom", "from_date": "2026-11-17", "to_date": "2026-11-18",
            "sender": "someone@example.test", "unread_only": True}
    query, info = gmail_filters("subject:meeting", now=AFTER, scope="received", filters=args)
    assert "{-in:sent in:inbox}" in query and "-in:drafts" in query
    assert 'from:"someone@example.test"' in query and "is:unread" in query
    assert info["range_ist"] == {"from": "2026-11-17", "through": "2026-11-18"}
    assert "in:sent" in gmail_filters("", now=AFTER, scope="sent", filters={})[0]
    with pytest.raises(ValueError):
        gmail_filters("after:2026/11/18", now=AFTER, scope="received", filters=args)
    with pytest.raises(ValueError):
        date_window(AFTER, "kal")


def test_count_pages_deduplicates_ignores_estimates_and_never_gets_messages():
    service = Mock()
    api = service.users.return_value.messages.return_value
    api.list.return_value.execute.side_effect = [
        {"messages": [{"id": "a"}, {"id": "b"}], "nextPageToken": "next", "resultSizeEstimate": 37},
        {"messages": [{"id": "b"}, {"id": "c"}], "resultSizeEstimate": 999},
    ]
    provider = GmailProvider(service)
    assert provider.count_matching_ids("in:sent", inbox=False, deadline=time.monotonic()+9) == {
        "complete": True, "count": 3}
    assert api.list.call_args.kwargs["pageToken"] == "next"
    assert api.list.call_args.kwargs["fields"] == "messages(id),nextPageToken"
    assert api.list.call_args.kwargs["includeSpamTrash"] is False
    assert not api.get.called
    api.list.return_value.execute.side_effect = [
        {"messages": [{"id": "a"}], "nextPageToken": "same"},
        {"messages": [{"id": "b"}], "nextPageToken": "same"},
    ]
    assert provider.count_matching_ids("", inbox=True, deadline=time.monotonic()+9) == {"complete": False}
    assert api.list.call_args.kwargs["labelIds"] == ["INBOX"]
    api.list.return_value.execute.side_effect = [{}]
    assert provider.count_matching_ids("", inbox=True, deadline=time.monotonic()+9) == {"complete": True, "count": 0}
    assert provider.count_matching_ids("", inbox=True, deadline=0) == {"complete": False}


def test_search_and_count_share_filters_preserve_inbox_cache_and_cross_midnight():
    async def run():
        provider = Mock(spec=GmailProvider)
        provider.search_metadata.return_value = {"messages": [
            {"id": "old", "sender": "Sanjay <sender@example.test>", "date": BEFORE.isoformat()},
            {"id": "new", "sender": "Sanjay <sender@example.test>", "date": AFTER.isoformat()}],
            "complete": False}
        provider.count_matching_ids.return_value = {"count": 3, "complete": True}
        provider.get_message.return_value = {"message_id": "new", "received_at": AFTER.isoformat(),
                                             "labels": ["UNREAD"], "body": "Verified text"}
        clock = [BEFORE]
        tools = GmailCallTools(lambda: provider, ist_counts=True, wall_clock=lambda: clock[0])
        args = {"range": "today", "sender": "sender@example.test", "unread_only": True}
        try:
            await tools.search(max_results=20, publish=False)
            assert (await tools.search())["cached"]
            assert provider.search_metadata.call_args.args[0] == ""
            assert "total_found" not in await tools.search(**args)
            assert await tools.count_mail(**args) == {
                "now_ist": "18 November 2026, 11:59 PM IST", "scope": "received",
                "range_ist": {"from": "2026-11-18", "through": "2026-11-18"},
                "count": 3, "complete": True}
            assert provider.search_metadata.call_args.args[0] == provider.count_matching_ids.call_args.args[0]
            answer = await tools.search(**args)
            assert answer["total_found"] == 3
            assert answer["messages"][0]["id"] == "new"
            assert answer["messages"][0]["sender_name"] == "Sanjay"
            assert answer["messages"][0]["sender_email"] == "sender@example.test"
            assert "19 November 2026, 12:00 AM IST" == answer["messages"][0]["date"]
            clock[0] = AFTER
            answer = await tools.search(**args)
            assert not answer["cached"] and "total_found" not in answer
            assert answer["range_ist"]["from"] == "2026-11-19"
            assert (await tools.read("new"))["message"]["unread"]
            provider.count_matching_ids.side_effect = TimeoutError
            assert "count" not in await tools.count_mail(**args)
        finally:
            tools.clear()
    asyncio.run(run())


def test_voice_count_is_opt_in_and_gmail_errors_never_return_partial_count():
    async def run():
        old = GmailConversationLive(None, Mock(), Mock(), Mock(), Mock())
        new = GmailConversationLive(None, Mock(), Mock(), Mock(), Mock(), ist_counts=True)
        try:
            old.socket = AsyncMock()
            new.socket = AsyncMock()
            import json
            for live, expected in ((old, 520), (new, 800)):
                await live.send({"setup": {}})
                setup = json.loads(live.socket.send.await_args.args[0])["setup"]
                assert setup["realtimeInputConfig"]["automaticActivityDetection"]["silenceDurationMs"] == expected
                tools = [item["name"] for item in setup["tools"][0]["functionDeclarations"]]
                assert ("count_mail" in tools) is (live is new)
            new.tools._request = AsyncMock(side_effect=OSError("provider private detail"))
            missing = await new._execute({"name": "count_mail", "args": {}})
            assert not missing["complete"] and "count" not in missing
            new.user_text = "How many did I receive this week?"
            mismatch = await new._execute({"name": "count_mail", "args": {"range": "all_time"}})
            assert not mismatch["complete"] and "count" not in mismatch
            new.tools._request.assert_not_awaited()
            new.user_text = "kal ke emails kitne hain?"
            ambiguous = await new._execute({"name": "count_mail", "args": {"range": "yesterday"}})
            assert "yesterday or tomorrow" in ambiguous["error"]
            new.tools._request.assert_not_awaited()
            new.user_text = "How many today?"
            call = {"id": "count", "name": "count_mail", "args": {"range": "today"}}
            with pytest.raises(ValueError):
                await old._execute(call)
            await new._answer_tools([call], new.generation)
            reply = json.loads(new.socket.send.await_args.args[0])["toolResponse"]["functionResponses"][0]["response"]
            assert reply["complete"] is False and "count" not in reply
            assert "private detail" not in str(reply)
        finally:
            await old.close()
            await new.close()
    asyncio.run(run())
