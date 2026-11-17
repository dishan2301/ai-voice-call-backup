import base64
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock
import pytest

from app.email.gmail import GMAIL_SCOPES, GmailProvider, gmail_message_to_mail


def _encoded(value: str) -> str:
    return base64.urlsafe_b64encode(value.encode()).decode().rstrip("=")


def _gmail_message(message_id: str = "gmail-1"):
    received = datetime(2026, 9, 14, 8, 0, tzinfo=timezone.utc)
    return {
        "id": message_id,
        "threadId": "thread-1",
        "internalDate": str(int(received.timestamp() * 1000)),
        "labelIds": ["INBOX"],
        "snippet": "Please review PO-42",
        "payload": {
            "mimeType": "multipart/mixed",
            "headers": [
                {"name": "From", "value": "Buyer <buyer@example.com>"},
                {"name": "To", "value": "Owner <owner@example.com>"},
                {"name": "Cc", "value": "Accounts <accounts@example.com>"},
                {"name": "Subject", "value": "Purchase Order PO-42"},
            ],
            "parts": [
                {
                    "mimeType": "text/plain",
                    "filename": "",
                    "body": {"data": _encoded("Please review PO-42"), "size": 19},
                },
                {
                    "mimeType": "application/pdf",
                    "filename": "PO-42.pdf",
                    "body": {"attachmentId": "must-not-fetch", "size": 12345},
                },
            ],
        },
    }


def test_gmail_scope_is_read_only_and_only_read_only() -> None:
    assert GMAIL_SCOPES == ["https://www.googleapis.com/auth/gmail.readonly"]


def test_gmail_conversion_extracts_body_and_attachment_metadata_only() -> None:
    message = gmail_message_to_mail(_gmail_message())

    assert message.sender_name == "Buyer"
    assert message.sender_email == "buyer@example.com"
    assert message.recipient_emails == ["owner@example.com"]
    assert message.cc_emails == ["accounts@example.com"]
    assert message.body_text == "Please review PO-42"
    assert message.attachments[0].model_dump() == {
        "filename": "PO-42.pdf",
        "mime_type": "application/pdf",
        "size": 12345,
    }
    assert "attachmentId" not in message.model_dump_json()


def test_gmail_conversion_indexes_small_inline_text_attachment() -> None:
    raw = _gmail_message("text-attachment")
    raw["payload"]["parts"].append({
        "mimeType": "text/csv", "filename": "attendance.csv",
        "body": {"data": _encoded("employee_id,status\n42,missing")},
    })

    message = gmail_message_to_mail(raw)

    assert "attendance.csv" in message.body_text
    assert "42,missing" in message.body_text
    assert message.attachments[-1].filename == "attendance.csv"


def test_attachment_id_is_kept_for_deferred_worker_fetch_without_exposing_it_in_schema():
    raw = _gmail_message("deferred-attachment")
    raw["payload"]["parts"].append({
        "mimeType": "application/pdf", "filename": "invoice.pdf",
        "body": {"attachmentId": "att-42", "size": 1200},
    })

    attachment = gmail_message_to_mail(raw).attachments[-1]

    assert attachment.attachment_id == "att-42"
    assert "att-42" not in attachment.model_dump_json()


def test_attachment_download_applies_size_limit():
    api = MagicMock()
    api.get.return_value.execute.return_value = {
        "size": 3, "data": _encoded("pdf")
    }
    service = MagicMock()
    service.users.return_value.messages.return_value.attachments.return_value = api

    result = GmailProvider(service).download_attachment("message-1", "att-1", max_size=3)

    assert result == b"pdf"
    api.get.assert_called_once_with(userId="me", messageId="message-1", id="att-1")


def test_gmail_provider_paginates_and_fetches_full_messages_read_only() -> None:
    messages_api = MagicMock()
    messages_api.list.return_value.execute.side_effect = [
        {"messages": [{"id": "gmail-1"}], "nextPageToken": "next"},
        {"messages": [{"id": "gmail-2"}]},
    ]
    messages_api.get.return_value.execute.side_effect = [
        _gmail_message("gmail-1"),
        _gmail_message("gmail-2"),
    ]
    service = MagicMock()
    service.users.return_value.messages.return_value = messages_api
    provider = GmailProvider(service)
    since = datetime(2026, 9, 14, 0, 0, tzinfo=timezone.utc)

    result = provider.fetch_messages(since, since + timedelta(days=1))

    assert [message.message_id for message in result] == ["gmail-1", "gmail-2"]
    assert messages_api.list.call_count == 2
    messages_api.get.assert_any_call(userId="me", id="gmail-1", format="full")
    messages_api.send.assert_not_called()


def test_full_sync_fetches_all_pages_without_a_date_filter_or_duplicate_ids() -> None:
    messages_api = MagicMock()
    messages_api.list.return_value.execute.side_effect = [
        {"messages": [{"id": "gmail-1"}], "nextPageToken": "next"},
        {"messages": [{"id": "gmail-1"}, {"id": "gmail-2"}]},
    ]
    messages_api.get.return_value.execute.side_effect = [
        _gmail_message("gmail-1"), _gmail_message("gmail-2")
    ]
    service = MagicMock()
    service.users.return_value.messages.return_value = messages_api

    result = GmailProvider(service).fetch_all_messages()

    assert [message.message_id for message in result] == ["gmail-1", "gmail-2"]
    assert messages_api.list.call_count == 2
    assert messages_api.list.call_args_list[0].kwargs == {
        "userId": "me", "maxResults": 100, "pageToken": None,
        "includeSpamTrash": True,
    }
    assert messages_api.get.call_count == 2


def test_full_sync_iterator_yields_each_page_without_accumulating_the_mailbox():
    messages_api = MagicMock()
    messages_api.list.return_value.execute.side_effect = [
        {"messages": [{"id": "gmail-1"}], "nextPageToken": "next"},
        {"messages": [{"id": "gmail-2"}]},
    ]
    messages_api.get.return_value.execute.side_effect = [
        _gmail_message("gmail-1"), _gmail_message("gmail-2")
    ]
    service = MagicMock()
    service.users.return_value.messages.return_value = messages_api
    pages = iter(GmailProvider(service).iter_all_messages())
    assert [mail.message_id for mail in next(pages)] == ["gmail-1"]
    assert messages_api.list.call_count == 1
    assert [mail.message_id for mail in next(pages)] == ["gmail-2"]
    assert messages_api.list.call_count == 2


def test_agent_search_page_preserves_query_cursor_and_later_body_text():
    api = MagicMock()
    api.list.return_value.execute.return_value = {
        'messages': [{'id': 'late'}], 'nextPageToken': 'another-page'}
    raw = _gmail_message('late')
    raw['payload']['parts'][0]['body']['data'] = _encoded('Intro ' * 1500 + 'The rare topic appears here.')
    api.get.return_value.execute.return_value = raw
    service = MagicMock()
    service.users.return_value.messages.return_value = api
    result = GmailProvider(service).search_page('"rare topic"', 10, 'previous-page')
    api.list.assert_called_once_with(userId='me', q='"rare topic"', maxResults=10, pageToken='previous-page')
    assert result['next_page_token'] == 'another-page'
    assert 'rare topic appears here' in result['messages'][0]['body']


def test_agent_search_page_converts_html_only_body_to_text():
    api = MagicMock()
    api.list.return_value.execute.return_value = {'messages': [{'id': 'html-only'}]}
    raw = _gmail_message('html-only')
    raw['payload']['parts'] = [{
        'mimeType': 'text/html', 'filename': '',
        'body': {'data': _encoded('<html><head><style>hidden</style></head><body><p>Hello <b>world</b>.</p></body></html>')},
    }]
    api.get.return_value.execute.return_value = raw
    service = MagicMock()
    service.users.return_value.messages.return_value = api

    body = GmailProvider(service).search_page('hello')['messages'][0]['body']

    assert 'Hello world.' in body
    assert '<' not in body
    assert 'hidden' not in body


def test_search_refreshes_and_persists_expired_read_token(tmp_path):
    api = MagicMock()
    api.list.return_value.execute.return_value = {'messages': []}
    service = MagicMock()
    service.users.return_value.messages.return_value = api
    credentials = service._http.credentials
    credentials.expired = True
    credentials.refresh_token = 'test-refresh-token'
    credentials.to_json.return_value = '{"token":"synthetic"}'
    from app.core.config import Settings

    token_path = tmp_path / 'read_token.json'
    GmailProvider(service, settings=Settings(google_token_file=str(token_path))).search_page('hello')

    credentials.refresh.assert_called_once()
    assert token_path.read_text() == '{"token":"synthetic"}'
    assert token_path.stat().st_mode & 0o777 == 0o600


def test_gmail_watch_requires_fully_qualified_topic_and_stops_cleanly():
    service = MagicMock()
    provider = GmailProvider(service)
    with pytest.raises(ValueError, match="fully qualified"):
        provider.watch_mailbox("topic-name")
    service.users.return_value.watch.return_value.execute.return_value = {
        "historyId": "42", "expiration": "999"
    }
    assert provider.watch_mailbox("projects/demo/topics/mail")['historyId'] == "42"
    service.users.return_value.watch.assert_called_once()
    provider.stop_mailbox_watch()
    service.users.return_value.stop.assert_called_once_with(userId="me")
