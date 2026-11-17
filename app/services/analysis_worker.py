"""Bounded durable analysis worker entrypoint."""
from app.api.dependencies import create_generation_service


def run_analysis_jobs() -> None:
    service = create_generation_service()
    topic = getattr(service.settings, "gmail_pubsub_topic", "")
    if topic and hasattr(service.provider, "watch_mailbox"):
        service.renew_gmail_watch_if_needed(service.provider, topic)
    service.run_analysis_worker(limit=20)
    if hasattr(service.provider, "download_attachment"):
        service.run_attachment_worker(service.provider, limit=10)
