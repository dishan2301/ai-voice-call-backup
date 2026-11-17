# AI Voice Call

Morning Mail PA: a Python/FastAPI voice and mail assistant with Gemini Live, owner-bound read-only Outlook integration, and an optional isolated SIP/AudioSocket pilot.

## Public source snapshot

This repository has fresh history. It contains application source and synthetic tests, not the original workspace's Git history, credentials, mailbox databases, local Asterisk runtime, OAuth tokens, APKs or private troubleshooting reports. Publishing code does not publish access to an Outlook account.

## Free Render preview

[Deploy this preview on Render](https://render.com/deploy?repo=https://github.com/dishan2301/ai-voice-call)

The Blueprint selects only the free web-service plan. The public root page reports deployment status and `/health` checks database access. Private application routes retain hosted authentication. Access logs are disabled to avoid recording OAuth callback query strings.

Free Render services sleep after inactivity and have no persistent disk. Local database state and account connections may be lost on restart or deployment. This preview generates a fresh database encryption key only when creating a fresh database; it refuses to replace a missing key for an existing database. Do not use this configuration as durable mailbox hosting.

No provider key is needed to start the preview. Outlook is disabled by default. Do not put credentials into Git or Docker build arguments. Configure private secrets in the host's environment only after choosing durable storage, encryption-key persistence, allowed mailbox identities and an HTTPS Microsoft callback URI. Sign in through OAuth on the new origin; do not copy a laptop's session cookie or token database into a public image.

## Voice limitations

The existing browser transport is WebRTC. Hosting the website does not itself relay audio: different-network/mobile-data operation needs a TURN relay and compatible hosting. This free preview does not expose Asterisk or public SIP/RTP ports. The direct-tool SIP conversation pilot remains separate from the browser transport. A working `/health` response does not certify remote voice or Outlook connectivity.

## Local development

Use Python 3.11 and install `requirements-dev.txt` in a virtual environment. Set test-only environment variables and run:

```sh
DEPLOYMENT_MODE=personal DATABASE_KEY_FILE='' EMAIL_PROVIDER=gmail python -m pytest -q
```

Optional PostgreSQL integration tests require an explicit isolated test database and may otherwise skip. Do not run tests against a real mailbox database.

## Container preview

```sh
docker build -t ai-voice-call .
docker run --rm -p 127.0.0.1:10000:10000 ai-voice-call
```

Production requires durable encrypted storage, persistent credential keys, owner authentication, explicit hostnames, provider configuration and real-device voice/security verification. Outlook sending remains disabled.
