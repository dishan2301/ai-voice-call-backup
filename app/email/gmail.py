import base64
import logging
import re
import time
from email.message import EmailMessage
from datetime import datetime, timezone
from email.utils import getaddresses, parseaddr
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from google.auth.transport.requests import Request
from google.auth.exceptions import RefreshError
from google_auth_httplib2 import AuthorizedHttp
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
import httplib2

from app.core.config import PROJECT_ROOT, Settings
from app.email.base import EmailProvider
from app.email.normalizer import html_to_text
from app.schemas.mail import AttachmentMetadata, MailMessage


GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
GMAIL_SEND_SCOPE = "https://www.googleapis.com/auth/gmail.send"
logger = logging.getLogger(__name__)


class GmailConfigurationError(RuntimeError):
    pass


def _project_path(value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else PROJECT_ROOT / path


def load_gmail_credentials(settings: Settings) -> Credentials:
    if settings.deployment_mode == "hosted":
        raise GmailConfigurationError("Hosted Gmail requires owned mailbox credentials")
    token_path = _project_path(settings.google_token_file)
    if not token_path.exists():
        raise GmailConfigurationError(
            "Gmail OAuth token is missing; run scripts/gmail_oauth_setup.py first"
        )
    credentials = Credentials.from_authorized_user_file(str(token_path))
    if credentials.expired and credentials.refresh_token:
        credentials.refresh(Request())
        token_path.write_text(credentials.to_json(), encoding="utf-8")
        token_path.chmod(0o600)
    if not credentials.valid:
        raise GmailConfigurationError(
            "Gmail OAuth token is invalid; run scripts/gmail_oauth_setup.py again"
        )
    return credentials


def create_gmail_service(settings: Settings):
    credentials = load_gmail_credentials(settings)
    # Bound every Gmail network call so a blocked LAN/DNS route cannot leave
    # browser refresh stuck on an old cached briefing indefinitely.
    http = AuthorizedHttp(credentials, http=httplib2.Http(timeout=15))
    return build(
        "gmail",
        "v1",
        http=http,
        cache_discovery=False,
    )


def _decode_body(data: str) -> str:
    if not data:
        return ""
    padding = "=" * (-len(data) % 4)
    try:
        return base64.urlsafe_b64decode(data + padding).decode("utf-8", errors="replace")
    except (ValueError, TypeError):
        return ""


def _decode_bytes(data: str) -> bytes:
    if not data:
        return b""
    padding = "=" * (-len(data) % 4)
    try:
        return base64.urlsafe_b64decode(data + padding)
    except (ValueError, TypeError):
        return b""


def _walk_parts(part: Dict[str, Any]) -> Iterable[Dict[str, Any]]:
    yield part
    for child in part.get("parts", []):
        yield from _walk_parts(child)


def _message_content(payload: Dict[str, Any]) -> Tuple[str, List[AttachmentMetadata]]:
    plain: List[str] = []
    html: List[str] = []
    attachments: List[AttachmentMetadata] = []
    for part in _walk_parts(payload):
        filename = part.get("filename", "")
        mime_type = part.get("mimeType", "application/octet-stream")
        body = part.get("body", {})
        if filename:
            attachments.append(
                AttachmentMetadata(
                    filename=filename,
                    mime_type=mime_type,
                    size=int(body.get("size", 0)),
                    attachment_id=str(body.get("attachmentId", "")),
                )
            )
            # Gmail includes small text attachments inline. Keep extraction
            # bounded and allowlisted; attachment IDs and binary payloads are
            # intentionally not fetched during the sync critical path.
            if mime_type in {"text/plain", "text/csv"} and int(body.get("size", 0)) <= 200_000:
                attachment_data = body.get("data", "")
                if attachment_data:
                    plain.append(f"\n[Attachment: {filename}]\n{_decode_body(attachment_data)}")
            continue
        data = body.get("data", "")
        if mime_type == "text/plain" and data:
            plain.append(_decode_body(data))
        elif mime_type == "text/html" and data:
            html.append(_decode_body(data))
    return "\n".join(plain or html), attachments


def gmail_message_to_mail(raw: Dict[str, Any]) -> MailMessage:
    payload = raw.get("payload", {})
    headers = {
        header.get("name", "").lower(): header.get("value", "")
        for header in payload.get("headers", [])
    }
    sender_name, sender_email = parseaddr(headers.get("from", ""))
    recipients = [address for _, address in getaddresses([headers.get("to", "")]) if address]
    cc = [address for _, address in getaddresses([headers.get("cc", "")]) if address]
    body_text, attachments = _message_content(payload)
    logger.debug(
        "gmail_body_parsed",
        extra={
            "message_id": raw.get("id"),
            "body_length": len(body_text),
            "has_body": bool(body_text.strip()),
        },
    )
    received_at = datetime.fromtimestamp(
        int(raw.get("internalDate", "0")) / 1000,
        tz=timezone.utc,
    )
    message_id = str(raw.get("id", ""))
    return MailMessage(
        message_id=message_id,
        thread_id=str(raw.get("threadId") or message_id),
        sender_name=sender_name,
        sender_email=sender_email,
        recipient_emails=recipients,
        cc_emails=cc,
        subject=headers.get("subject", ""),
        received_at=received_at,
        body_text=body_text,
        snippet=raw.get("snippet", ""),
        labels=raw.get("labelIds", []),
        gmail_history_id=str(raw.get("historyId") or ""),
        has_attachments=bool(attachments),
        attachments=attachments,
    )


def _header(raw: Dict[str, Any], name: str) -> str:
    return next((str(header.get("value", "")) for header in raw.get("payload", {}).get("headers", [])
                 if str(header.get("name", "")).lower() == name.lower()), "")


class GmailProvider(EmailProvider):
    """Gmail API read-only transport adapter."""

    def __init__(self, service: Any, send_service=None, settings: Optional[Settings] = None,
                 persist_read_credential: Optional[Callable[[str], None]] = None,
                 persist_send_credential: Optional[Callable[[str], None]] = None):
        self.service = service
        self.send_service = send_service
        self.settings = settings
        self.persist_read_credential = persist_read_credential
        self.persist_send_credential = persist_send_credential

    def _refresh_credentials(self, credentials, persist=None, token_file: Optional[str] = None) -> None:
        if not credentials or not credentials.expired or not credentials.refresh_token:
            return
        try:
            credentials.refresh(Request())
            if persist:
                persist(credentials.to_json())
            elif token_file:
                token_path = _project_path(token_file)
                token_path.write_text(credentials.to_json(), encoding="utf-8")
                token_path.chmod(0o600)
        except RefreshError as exc:
            raise GmailConfigurationError(
                "Gmail OAuth token could not be refreshed; re-run scripts/gmail_oauth_setup.py"
            ) from exc

    def _ensure_fresh_token(self) -> None:
        """Refresh the read token before a Gmail request and persist it."""
        credentials = getattr(getattr(self.service, "_http", None), "credentials", None)
        self._refresh_credentials(
            credentials, self.persist_read_credential,
            self.settings.google_token_file if self.settings and not self.persist_read_credential else None,
        )

    def send_message(self, draft):
        if self.send_service is None:
            raise GmailConfigurationError("Separate Gmail send authorization is required.")
        recipient = str(draft.get("to", "")).strip()
        subject = str(draft.get("subject", "")).strip()
        body = str(draft.get("body", ""))
        if not re.fullmatch(r"[^\s@,<>;]+@[^\s@,<>;]+\.[^\s@,<>;]+", recipient):
            raise ValueError("A single verified recipient address is required")
        if not subject or not body or any(c in subject for c in "\r\n"):
            raise ValueError("A valid subject and message body are required")
        message = EmailMessage()
        message["To"] = recipient
        message["Subject"] = subject
        if draft.get("in_reply_to"):
            message["In-Reply-To"] = str(draft["in_reply_to"])
        if draft.get("references"):
            message["References"] = str(draft["references"])
        message.set_content(body)
        payload = {"raw": base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")}
        if draft.get("thread_id"):
            payload["threadId"] = str(draft["thread_id"])
        service = self.send_service() if callable(self.send_service) else self.send_service
        credentials = getattr(getattr(service, "_http", None), "credentials", None)
        self._refresh_credentials(credentials, self.persist_send_credential)
        # A separately authorized SEND token must belong to the same Gmail
        # account as the verified READ token before any message is submitted.
        read_identity = self.service.users().getProfile(userId="me").execute().get("emailAddress", "").casefold()
        send_identity = service.users().getProfile(userId="me").execute().get("emailAddress", "").casefold()
        if not read_identity or read_identity != send_identity:
            raise GmailConfigurationError("Gmail send authorization belongs to a different account.")
        request = service.users().messages().send(userId="me", body=payload)
        return request.execute(num_retries=0)

    def download_attachment(self, message_id: str, attachment_id: str, max_size: int = 10_000_000) -> bytes:
        """Fetch one bounded attachment only after a worker explicitly requests it."""
        if not attachment_id:
            raise ValueError("Attachment ID is required")
        response = self.service.users().messages().attachments().get(
            userId="me", messageId=message_id, id=attachment_id
        ).execute()
        size = int(response.get("size", 0))
        if size > max_size:
            raise ValueError("Attachment exceeds the configured size limit")
        data = _decode_bytes(response.get("data", ""))
        if len(data) > max_size:
            raise ValueError("Attachment exceeds the configured size limit")
        return data

    def watch_mailbox(self, topic_name: str, label_ids: Optional[List[str]] = None) -> Dict[str, str]:
        """Register Gmail push notifications; callers must still run history sync."""
        topic = str(topic_name).strip()
        if not topic.startswith("projects/") or "/topics/" not in topic:
            raise ValueError("A fully qualified Pub/Sub topic is required")
        body: Dict[str, Any] = {"topicName": topic}
        if label_ids:
            body["labelIds"] = [str(label) for label in label_ids if str(label).strip()]
        return self.service.users().watch(userId="me", body=body).execute()

    def stop_mailbox_watch(self) -> None:
        """Stop the current Gmail watch; notification receipt never mutates mail."""
        self.service.users().stop(userId="me").execute()

    @classmethod
    def from_settings(cls, settings: Settings) -> "GmailProvider":
        if settings.deployment_mode == "hosted":
            raise GmailConfigurationError("Hosted Gmail requires owned mailbox credentials")
        def send_service():
            if _project_path(settings.google_send_token_file).resolve() == _project_path(settings.google_token_file).resolve():
                raise GmailConfigurationError("Read and send tokens must be separate.")
            send_settings = settings.model_copy(update={"google_token_file": settings.google_send_token_file})
            credentials = load_gmail_credentials(send_settings)
            if not credentials.has_scopes([GMAIL_SEND_SCOPE]):
                raise GmailConfigurationError("The separate token does not authorize sending.")
            return build("gmail", "v1", credentials=credentials, cache_discovery=False)
        return cls(create_gmail_service(settings), send_service=send_service, settings=settings)

    @classmethod
    def from_mailbox(cls, settings, context, session_factory):
        from app.email.mailbox_gmail import MailboxGmailHttp
        transport = MailboxGmailHttp(settings, context, session_factory)
        return cls(build("gmail", "v1", http=transport, cache_discovery=False, static_discovery=True))

    @classmethod
    def from_authorized_user_info(cls, read_info: dict, read_scopes: List[str], settings: Settings,
                                  *, send_info: Optional[dict] = None,
                                  send_scopes: Optional[List[str]] = None,
                                  persist_read_credential=None,
                                  persist_send_credential=None) -> "GmailProvider":
        """Build a hosted client from one mailbox's encrypted OAuth grants."""
        read_scopes = sorted(set(read_scopes))
        if set(read_scopes) != set(GMAIL_SCOPES):
            raise GmailConfigurationError("Mailbox READ authorization has incorrect scopes.")
        read_credentials = Credentials.from_authorized_user_info(read_info, scopes=read_scopes)
        if not read_credentials.valid and not (read_credentials.expired and read_credentials.refresh_token):
            raise GmailConfigurationError("Mailbox READ authorization is invalid or expired.")
        read_http = AuthorizedHttp(read_credentials, http=httplib2.Http(timeout=15))
        read_service = build("gmail", "v1", http=read_http, cache_discovery=False)
        send_service = None
        if send_info is not None:
            send_scopes = sorted(set(send_scopes or []))
            if set(send_scopes) != {GMAIL_SEND_SCOPE}:
                raise GmailConfigurationError("Mailbox SEND authorization has incorrect scopes.")
            send_credentials = Credentials.from_authorized_user_info(send_info, scopes=send_scopes)
            if not send_credentials.valid and not (send_credentials.expired and send_credentials.refresh_token):
                raise GmailConfigurationError("Mailbox SEND authorization is invalid or expired.")
            send_http = AuthorizedHttp(send_credentials, http=httplib2.Http(timeout=15))
            send_service = build("gmail", "v1", http=send_http, cache_discovery=False)
        return cls(read_service, send_service=send_service, settings=settings,
                   persist_read_credential=persist_read_credential,
                   persist_send_credential=persist_send_credential)

    def fetch_messages(self, since: datetime, until: datetime) -> List[MailMessage]:
        self._ensure_fresh_token()
        if since.tzinfo is None or until.tzinfo is None:
            raise ValueError("Gmail fetch bounds must be timezone-aware")
        query = f"after:{int(since.timestamp())} before:{int(until.timestamp())}"
        summaries: List[Dict[str, str]] = []
        page_token: Optional[str] = None
        while True:
            response = (
                self.service.users()
                .messages()
                .list(userId="me", q=query, pageToken=page_token, maxResults=100)
                .execute()
            )
            summaries.extend(response.get("messages", []))
            page_token = response.get("nextPageToken")
            if not page_token:
                break

        messages: List[MailMessage] = []
        for summary in summaries:
            raw = (
                self.service.users()
                .messages()
                .get(userId="me", id=summary["id"], format="full")
                .execute()
            )
            message = gmail_message_to_mail(raw)
            if since <= message.received_at < until:
                messages.append(message)
        return messages

    def iter_all_messages(self):
        """Yield one bounded Gmail page of normalized messages at a time."""
        seen_ids = set()
        page_token = None
        while True:
            messages: List[MailMessage] = []
            response = self.service.users().messages().list(
                userId="me", maxResults=100, pageToken=page_token, includeSpamTrash=True
            ).execute()
            for summary in response.get("messages", []):
                message_id = str(summary["id"])
                if message_id in seen_ids:
                    continue
                seen_ids.add(message_id)
                try:
                    raw = self.service.users().messages().get(
                        userId="me", id=message_id, format="full"
                    ).execute()
                except HttpError as exc:
                    if getattr(exc.resp, "status", None) == 404:
                        continue  # Deleted between list and get; the next history pass reconciles it.
                    raise
                messages.append(gmail_message_to_mail(raw))
            if messages:
                yield messages
            page_token = response.get("nextPageToken")
            if not page_token:
                return

    def fetch_all_messages(self) -> List[MailMessage]:
        """Compatibility wrapper; production sync consumes bounded pages."""
        return [message for page in self.iter_all_messages() for message in page]

    def current_history_id(self) -> str:
        """Return Gmail's current checkpoint without downloading messages."""
        profile = self.service.users().getProfile(userId="me").execute()
        return str(profile.get("historyId") or "")

    def fetch_incremental(self, start_history_id: str) -> Tuple[List[MailMessage], List[str], str]:
        """Fetch changed messages after a committed Gmail history checkpoint.

        Returns current message records, message IDs reported as deleted, and
        Gmail's newest history ID. Callers must persist the checkpoint only
        after the returned records have been committed successfully.
        """
        if not start_history_id:
            raise ValueError("A committed Gmail history ID is required")
        history_api = self.service.users().history()
        page_token = None
        changed_ids = set()
        deleted_ids = set()
        newest_history_id = start_history_id
        while True:
            response = history_api.list(
                userId="me", startHistoryId=start_history_id,
                historyTypes=["messageAdded", "messageDeleted", "labelAdded", "labelRemoved"],
                pageToken=page_token,
            ).execute()
            newest_history_id = str(response.get("historyId") or newest_history_id)
            for row in response.get("history", []):
                for key in ("messagesAdded", "labelsAdded", "labelsRemoved"):
                    for item in row.get(key, []):
                        message = item.get("message", item)
                        if message.get("id"):
                            changed_ids.add(str(message["id"]))
                for item in row.get("messagesDeleted", []):
                    message = item.get("message", item)
                    if message.get("id"):
                        deleted_ids.add(str(message["id"]))
            page_token = response.get("nextPageToken")
            if not page_token:
                break
        messages = []
        for message_id in sorted(changed_ids - deleted_ids):
            try:
                raw = self.service.users().messages().get(
                    userId="me", id=message_id, format="full"
                ).execute()
            except HttpError as exc:
                if getattr(exc.resp, "status", None) == 404:
                    deleted_ids.add(message_id)
                    continue
                raise
            messages.append(gmail_message_to_mail(raw))
        return messages, sorted(deleted_ids), newest_history_id

    def search(self, query: str, limit: int = 10) -> List[Dict[str, Any]]:
        return self.search_page(query, limit)["messages"]

    def search_metadata(self, query: str, limit: int, *, inbox: bool = False) -> Dict[str, Any]:
        """Read one bounded Gmail page and batch its headers for the SIP caller."""
        self._ensure_fresh_token()
        messages = self.service.users().messages()
        page = messages.list(userId="me", q=query or None, maxResults=limit,
                             labelIds=["INBOX"] if inbox else None).execute()
        ids = [item["id"] for item in page.get("messages", [])[:limit]]
        rows = {}
        if ids:
            def collect(request_id, raw, error):
                if error:
                    # Deleted between listing and fetch. The response is partial.
                    if getattr(getattr(error, "resp", None), "status", None) != 404:
                        raise error
                    return
                millis = int(raw["internalDate"])
                rows[request_id] = {
                    "id": request_id, "sender": _header(raw, "From")[:320],
                    "subject": _header(raw, "Subject")[:500],
                    "date": datetime.fromtimestamp(millis / 1000, timezone.utc).isoformat(),
                    "snippet": str(raw.get("snippet") or "")[:500],
                    "unread": "UNREAD" in raw.get("labelIds", []),
                }

            batch = self.service.new_batch_http_request(callback=collect)
            for message_id in ids:
                batch.add(messages.get(userId="me", id=message_id, format="metadata",
                                       metadataHeaders=["From", "Subject"],
                                       fields="id,internalDate,labelIds,snippet,payload(headers)"),
                          request_id=message_id)
            batch.execute()
        return {"messages": [rows[mid] for mid in ids if mid in rows],
                "complete": not page.get("nextPageToken") and len(rows) == len(ids)}

    def count_matching_ids(self, query: str, *, inbox: bool, deadline: float) -> Dict[str, Any]:
        """Count message IDs only, with no estimate on incomplete enumeration."""
        self._ensure_fresh_token()
        messages = self.service.users().messages()
        ids, tokens = set(), set()
        token = None
        for _ in range(40):
            if time.monotonic() >= deadline:
                break
            page = messages.list(
                userId="me", q=query or None, labelIds=["INBOX"] if inbox else None,
                includeSpamTrash=False, maxResults=500, pageToken=token,
                fields="messages(id),nextPageToken",
            ).execute()
            if time.monotonic() >= deadline:
                break
            ids.update(row["id"] for row in page.get("messages", []))
            token = page.get("nextPageToken")
            if not token:
                return {"complete": True, "count": len(ids)}
            if token in tokens:
                break
            tokens.add(token)
        return {"complete": False}

    def search_page(self, query: str, limit: int = 10, page_token: str = "") -> Dict[str, Any]:
        self._ensure_fresh_token()
        response = self.service.users().messages().list(
            userId="me", q=query, maxResults=limit, pageToken=page_token or None
        ).execute()
        results = []
        for item in response.get("messages", [])[:limit]:
            raw = self.service.users().messages().get(
                userId="me", id=item["id"], format="full"
            ).execute()
            mail = gmail_message_to_mail(raw)
            results.append({
                "message_id": mail.message_id, "rfc_message_id": _header(raw, "Message-ID"),
                "references": _header(raw, "References"),
                "thread_id": mail.thread_id,
                "sender": f"{mail.sender_name} <{mail.sender_email}>".strip(),
                "subject": mail.subject, "recipient_emails": mail.recipient_emails, "received_at": mail.received_at.isoformat(),
                "body": html_to_text(mail.body_text)[:40000], "body_truncated": len(mail.body_text) > 40000,
                "labels": mail.labels,
            })
        return {"messages": results, "next_page_token": response.get("nextPageToken", ""),
                "candidate_count": len(results)}

    def get_message(self, message_id: str) -> Dict[str, Any]:
        """Verify one index candidate against the currently authorized provider."""
        self._ensure_fresh_token()
        raw = self.service.users().messages().get(userId="me", id=message_id, format="full").execute()
        mail = gmail_message_to_mail(raw)
        return {"message_id": mail.message_id, "thread_id": mail.thread_id,
                "rfc_message_id": _header(raw, "Message-ID"), "references": _header(raw, "References"),
                "in_reply_to": _header(raw, "In-Reply-To"),
                "sender": f"{mail.sender_name} <{mail.sender_email}>".strip(), "subject": mail.subject,
                "recipient_emails": mail.recipient_emails, "received_at": mail.received_at.isoformat(),
                "body": html_to_text(mail.body_text)[:40000], "body_truncated": len(mail.body_text)>40000,
                "labels": mail.labels, "cc": mail.cc_emails,
                "attachments": [{"filename": item.filename, "mime_type": item.mime_type,
                    "size_bytes": item.size, "attachment_id": item.attachment_id} for item in mail.attachments]}

    def get_thread(self, thread_id: str, limit: int = 20) -> List[Dict[str, Any]]:
        raw_thread = self.service.users().threads().get(
            userId="me", id=thread_id, format="full"
        ).execute()
        rows = sorted(raw_thread.get("messages", []), key=lambda row: int(row.get("internalDate", 0)))
        results = []
        for raw in rows[-limit:]:
            mail = gmail_message_to_mail(raw)
            results.append({"message_id": mail.message_id, "rfc_message_id": _header(raw, "Message-ID"),
                            "references": _header(raw, "References"),
                            "thread_id": mail.thread_id,
                            "sender": f"{mail.sender_name} <{mail.sender_email}>".strip(),
                            "subject": mail.subject, "recipient_emails": mail.recipient_emails, "received_at": mail.received_at.isoformat(),
                            "body": mail.body_text[:40000], "thread_truncated": len(rows) > limit,
                            "thread_message_count": len(rows), "body_truncated": len(mail.body_text) > 40000,
                            "labels": mail.labels})
        return results

    def unanswered(self, days: int = 7, limit: int = 10) -> List[Dict[str, Any]]:
        self._ensure_fresh_token()
        candidates = self.search(f"in:sent older_than:{days}d", limit * 3)
        unanswered = []
        seen = set()
        for candidate in candidates:
            thread_id = candidate["thread_id"]
            if thread_id in seen:
                continue
            seen.add(thread_id)
            raw_thread = self.service.users().threads().get(
                userId="me", id=thread_id, format="metadata",
                metadataHeaders=["Subject"],
            ).execute()
            messages = sorted(raw_thread.get("messages", []), key=lambda row: int(row.get("internalDate", 0)))
            if messages and "SENT" in messages[-1].get("labelIds", []):
                unanswered.append({"thread_id": candidate["thread_id"],
                                   "message_id": candidate["message_id"],
                                   "subject": candidate["subject"],
                                   "last_message_at": datetime.fromtimestamp(
                                       int(messages[-1].get("internalDate", 0)) / 1000,
                                       tz=timezone.utc,
                                   ).isoformat()})
            if len(unanswered) >= limit:
                break
        return unanswered
