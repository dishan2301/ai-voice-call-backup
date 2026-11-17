import asyncio
from datetime import datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.agents.draft_safety import recover_stale_sends
from app.api.phone import phone_resume
from app.core.auth import MailboxContext
from app.database.db import Base
from app.database.models import AgentAction, AgentConversation


def test_interrupted_send_becomes_unknown_and_stays_owned_without_retry():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    now = datetime.utcnow()
    with sessions() as db:
        for mailbox in ("alice-mail", "bob-mail"):
            conversation = AgentConversation(mailbox_id=mailbox, session_key="shared-session-key",
                                             user_message="done", assistant_message="")
            db.add(conversation)
            db.flush()
            db.add(AgentAction(conversation_id=conversation.id, action_type="PREPARE_DRAFT",
                               arguments_json={"to": "customer@example.test", "subject": "Reply",
                                               "body": "Thanks."}, rationale="test", risk="HIGH",
                               status="SENDING", approved_at=now - timedelta(minutes=3)))
        db.commit()
        assert recover_stale_sends(db, now=now, mailbox_id="alice-mail") == 1
        assert recover_stale_sends(db, now=now, mailbox_id="alice-mail") == 0
        alice = asyncio.run(phone_resume("shared-session-key", db,
                                         MailboxContext("alice", "w", "alice-mail")))
        bob = db.query(AgentAction).join(AgentConversation).filter(
            AgentConversation.mailbox_id == "bob-mail").one()
        assert alice["send_outcome_unknown"]["draft_id"] != bob.id
        assert "Check Sent" in alice["send_outcome_unknown"]["message"]
        assert "draft" not in alice
        assert bob.status == "SENDING"
        assert recover_stale_sends(db, now=now) == 1
        assert db.query(AgentAction).filter_by(status="SEND_UNCONFIRMED").count() == 2
