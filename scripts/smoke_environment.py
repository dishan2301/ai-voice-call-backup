"""Validate live Gmail/Gemini/HTTPS prerequisites without exposing secrets."""
import argparse
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import get_settings
from app.email.gmail import GmailConfigurationError, create_gmail_service


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--https-url", help="deployed HTTPS health URL to probe")
    args = parser.parse_args()
    settings = get_settings()
    failures = []
    if not settings.gemini_api_key:
        failures.append("GEMINI_API_KEY is not configured")
    try:
        service = create_gmail_service(settings)
        service.users().getProfile(userId="me").execute(num_retries=0)
        print("gmail: ok")
    except GmailConfigurationError as exc:
        failures.append(str(exc))
    except Exception as exc:
        failures.append(f"Gmail request failed: {type(exc).__name__}")
    if args.https_url:
        try:
            with urllib.request.urlopen(args.https_url, timeout=10) as response:
                print(f"https: ok ({response.status})")
        except Exception as exc:
            failures.append(f"HTTPS probe failed: {type(exc).__name__}")
    else:
        print("https: skipped (pass --https-url to probe a deployed endpoint)")
    if failures:
        for failure in failures:
            print(f"check: {failure}", file=sys.stderr)
        return 1
    print("environment smoke checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
