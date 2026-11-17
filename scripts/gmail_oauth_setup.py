#!/usr/bin/env python3
import sys
import argparse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from google_auth_oauthlib.flow import InstalledAppFlow

from app.core.config import PROJECT_ROOT, get_settings
from app.email.gmail import GMAIL_SCOPES, GMAIL_SEND_SCOPE


def project_path(value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else PROJECT_ROOT / path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--enable-send", action="store_true", help="Authorize reviewed email sending")
    parser.add_argument("--enable-calendar", action="store_true", help="Authorize Calendar separately without replacing Gmail tokens")
    args = parser.parse_args()
    settings = get_settings()
    if args.enable_send and args.enable_calendar:
        parser.error("Authorize one capability at a time")
    client_path = project_path(settings.google_client_secret_file)
    token_path = project_path(settings.google_send_token_file if args.enable_send else settings.google_token_file)
    if args.enable_calendar:
        token_path = project_path(settings.google_calendar_token_file)
        if token_path in {project_path(settings.google_token_file), project_path(settings.google_send_token_file)}:
            raise SystemExit("Calendar token path must differ from Gmail token paths")
    if not client_path.exists():
        raise SystemExit(
            f"Google client secret not found at {client_path}. See README.md for setup."
        )
    scopes = [GMAIL_SEND_SCOPE] if args.enable_send else GMAIL_SCOPES
    if args.enable_calendar:
        from app.services.calendar_provider import CALENDAR_SCOPES
        scopes = CALENDAR_SCOPES
    flow = InstalledAppFlow.from_client_secrets_file(str(client_path), scopes)
    credentials = flow.run_local_server(port=0, open_browser=True)
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(credentials.to_json(), encoding="utf-8")
    token_path.chmod(0o600)
    print(f"Gmail authorization saved to {token_path} with private permissions.")


if __name__ == "__main__":
    main()
