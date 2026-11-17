"""Read-only Gmail transport resolving authorization for every owned mailbox request."""
import json
from urllib.parse import urlsplit

import httplib2
from google.auth.exceptions import RefreshError
from google.oauth2.credentials import Credentials
from google_auth_httplib2 import AuthorizedHttp, Request

from app.database.models import MailboxConnection
from app.email.gmail import GMAIL_SCOPES, GmailConfigurationError
from app.services.mailbox_credentials import MailboxCredentialStore, MailboxCredentialError


class MailboxGmailHttp:
    def __init__(self, settings, context, session_factory):
        if settings.deployment_mode != "hosted" or context.legacy_local or context.provider != "gmail":
            raise GmailConfigurationError("An owned hosted Gmail connection is required")
        self.context = context
        self.session_factory = session_factory
        self.store = MailboxCredentialStore(settings.provider_credential_key_directory,
                                            settings.provider_credential_key_version)
        self._snapshot()

    def _snapshot(self):
        with self.session_factory() as db:
            bundle = self.store.get(db, self.context, self.context.mailbox_id)
            ciphertext = db.get(MailboxConnection, self.context.mailbox_id).credential_ciphertext
        return bundle, ciphertext

    def request(self, uri, method="GET", body=None, headers=None, **kwargs):
        target = urlsplit(uri)
        if (method != "GET" or target.scheme != "https" or target.hostname != "gmail.googleapis.com"
                or target.username or target.password or target.port not in {None, 443}
                or not target.path.startswith("/gmail/v1/users/me/")):
            raise GmailConfigurationError("Hosted Gmail read transport refused this operation")
        bundle, ciphertext = self._snapshot()
        try:
            credentials = Credentials.from_authorized_user_info(bundle["read"])
        except (ValueError, TypeError, KeyError) as exc:
            raise GmailConfigurationError("Gmail connection needs reauthorization") from exc
        if not credentials.has_scopes(GMAIL_SCOPES):
            raise GmailConfigurationError("Gmail read permission is missing")
        http = httplib2.Http(timeout=15)
        try:
            if not credentials.valid:
                try:
                    credentials.refresh(Request(http))
                except RefreshError as exc:
                    raise GmailConfigurationError("Gmail connection needs reauthorization") from exc
                if not credentials.valid:
                    raise GmailConfigurationError("Gmail connection needs reauthorization")
                bundle["read"] = json.loads(credentials.to_json())
                with self.session_factory() as db:
                    self.store.put(db, self.context, self.context.mailbox_id, bundle,
                                   expected_ciphertext=ciphertext)
                    db.expire_all()
                    ciphertext = db.get(MailboxConnection, self.context.mailbox_id).credential_ciphertext
            if self._snapshot()[1] != ciphertext:
                raise MailboxCredentialError("Mailbox credential changed; retry the operation")
            # Redirects and automatic token retries must not bypass ownership checks.
            kwargs.pop("redirections", None)
            response = AuthorizedHttp(credentials, http=http, max_refresh_attempts=0).request(
                uri, method=method, body=body, headers=headers, redirections=0, **kwargs)
            if self._snapshot()[1] != ciphertext:
                raise MailboxCredentialError("Mailbox credential changed during the request")
            return response
        finally:
            http.close()
