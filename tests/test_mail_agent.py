from datetime import datetime, timezone, timedelta
import json

from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker

from app.agents.mail_agent import MailAgent
from app.core.config import Settings
from app.database.db import Base
from app.database.models import AgentAction, AgentAudit, AgentMemory, ConversationState, Email, EmailAnalysis, Attachment, SyncCheckpoint
from app.agents.draft_safety import begin_review, complete_review
from app.agents.requests import handle_mail_request
from app.llm.base import LLMProviderError


class FakeLLM:
    def __init__(self, decisions):
        self.decisions = iter(decisions)

    def generate_json(self, *args):
        if 'matches' in args[2].get('properties', {}):
            payload = json.loads(args[1])
            return {'requested_scope': 'operational',
                    'matches': [{'message_id': row['message_id'], 'excerpt': row['subject'],
                                 'relationship': 'operational', 'reason': 'The message concerns the request.'}
                                for row in payload['messages']]}
        value = next(self.decisions)
        if isinstance(value, Exception):
            raise value
        return value


class FakeGmail:
    def __init__(self):
        self.sent = []
        self.queries = []

    def search(self, query, limit):
        self.queries.append(query)
        return [{"sender": "Zydus <client@example.com>", "message_id": "m1",
                 "rfc_message_id": "<message-1@example.com>", "subject": "Payment", "thread_id": "thread-1",
                 "received_at": datetime.now(timezone.utc).isoformat(), "body": "Please confirm."}]

    def unanswered(self, days, limit):
        return [{"thread_id": "t2", "subject": "Awaiting reply"}]

    def get_thread(self, thread_id, limit):
        return [{"sender": "Zydus <client@example.com>", "message_id": "m1",
                 "rfc_message_id": "<message-1@example.com>", "subject": "Meeting",
                 "thread_id": thread_id, "received_at": datetime.now(timezone.utc).isoformat(),
                 "body": "Can we meet at 3 pm?"}]

    def send_message(self, draft):
        self.sent.append(draft)
        return {"id": "sent-1", "threadId": draft.get("thread_id", "new")}


def decision(kind="tool", tool="", arguments=None, answer="", rationale="needed"):
    return {"kind": kind, "plan": "inspect then answer", "rationale": rationale,
            "tool": tool, "arguments": arguments or {}, "answer": answer}


def make_agent(decisions):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    settings = Settings(gemini_api_key="test", email_provider="gmail")
    return MailAgent(settings, factory, FakeLLM(decisions), FakeGmail()), factory


def test_structured_intent_parser_extracts_semantic_constraints():
    assert MailAgent.parse_structured_intent("What urgent emails today need a reply?") == {
        "intent": "find_mail", "date_range": "today", "reply_required": True,
        "priorities": ["urgent"]}


def test_daily_received_count_uses_llm_selected_history_search():
    prompts = []

    class CountLLM:
        calls = 0

        def generate_json(self, _system, prompt, schema):
            payload = json.loads(prompt)
            if "matches" in schema.get("properties", {}):
                return {"requested_scope": "operational", "matches": [{
                    "message_id": row["message_id"], "excerpt": row["subject"],
                    "relationship": "operational", "reason": "The message matches today's date range."}
                    for row in payload["messages"]]}
            prompts.append(payload)
            self.calls += 1
            if self.calls == 1:
                return decision(tool="search_history", arguments={"text": "", "limit": 20})
            return decision(kind="final", answer="You received 2 emails today.")

    agent, factory = make_agent([])
    agent.llm = CountLLM()
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    with factory() as db:
        db.add(SyncCheckpoint(scope="gmail:default", status="ok", last_successful_sync_at=now))
        db.add_all([
            Email(provider="gmail", mailbox_id="default", provider_message_id="today-1",
                  thread_id="today-1", sender_name="One", sender_email="one@test",
                  subject="First", normalized_body="Hello", received_at=now,
                  body_hash="today-1", is_deleted=False),
            Email(provider="gmail", mailbox_id="default", provider_message_id="today-2",
                  thread_id="today-2", sender_name="Two", sender_email="two@test",
                  subject="Second", normalized_body="Hello", received_at=now - timedelta(hours=1),
                  body_hash="today-2", is_deleted=False),
            Email(provider="gmail", mailbox_id="default", provider_message_id="old-1",
                  thread_id="old-1", sender_name="Old", sender_email="old@test",
                  subject="Yesterday", normalized_body="Hello", received_at=now - timedelta(days=1),
                  body_hash="old-1", is_deleted=False),
        ])
        db.commit()
    result = agent.run("How many mail did I receive today?", "daily-count")
    assert result["answer"] == "You received 2 emails today."
    assert result["steps"] == 2
    assert {item["message_id"] for item in result["evidence"]} == {"today-1", "today-2"}
    assert prompts[0]["structured_intent"]["date_range"] == "today"
    assert agent.gmail.queries == []


def test_explicit_session_change_returns_a_fresh_session_key():
    result = handle_mail_request(lambda: None, "change the session now", "old-session-key")
    assert result["session_reset"] is True
    assert result["session_key"] != "old-session-key"
    assert result["state"] == "NEW_SESSION"


def test_universal_pre_retrieval_searches_original_wording_and_clues():
    agent, factory = make_agent([])
    with factory() as db:
        db.add(Email(provider="gmail", mailbox_id="default", provider_message_id="bio-1",
                     thread_id="thread-bio", sender_name="Operator", sender_email="operator@test",
                     subject="Device update", normalized_body="The biometric reader needs service.",
                     received_at=datetime.now(), body_hash="bio", is_deleted=False))
        db.commit()
        matches = agent._deterministic_local_retrieval(db, "Find mail related to biometric")
    assert [item["message_id"] for item in matches] == ["bio-1"]


def test_mail_source_time_is_given_to_agent_in_india_time():
    agent, _ = make_agent([])
    rows = [{"message_id": "local", "subject": "Meeting", "received_at": "2026-09-23T07:04:00"},
            {"message_id": "gmail", "subject": "Meeting", "received_at": "2026-09-23T07:04:00+00:00"}]
    prepared = agent._prepare_search_results(rows)
    assert [item["received_at"] for item in prepared] == ["2026-09-23T12:34:00+05:30"] * 2


def test_pre_retrieval_does_not_publish_unrelated_token_matches():
    agent, factory = make_agent([decision(kind="final", answer="I found the biometric reader request.")])
    with factory() as db:
        for ident, subject, body in [
            ("wanted", "Biometric reader", "The biometric reader needs service."),
            ("unrelated", "Quarterly readership report", "The reader survey budget was approved."),
        ]:
            db.add(Email(provider="gmail", mailbox_id="default", provider_message_id=ident,
                         thread_id=ident, sender_name="Operator", sender_email="operator@test",
                         subject=subject, normalized_body=body, received_at=datetime.now(),
                         body_hash=ident, is_deleted=False))
        db.commit()

    class RelevanceLLM:
        def generate_json(self, _system, prompt, schema):
            if "matches" not in schema.get("properties", {}):
                return decision(kind="final", answer="I found the biometric reader request.")
            rows = json.loads(prompt)["messages"]
            assert {row["message_id"] for row in rows} == {"wanted", "unrelated"}
            wanted = next(row for row in rows if row["message_id"] == "wanted")
            return {"requested_scope": "operational", "matches": [{
                "message_id": "wanted", "excerpt": wanted["subject"],
                "relationship": "operational", "reason": "The reader needs service."}]}

    agent.llm = RelevanceLLM()
    result = agent.run("Find mail about biometric reader", "relevant-local")

    assert [item["message_id"] for item in result["evidence"]] == ["wanted"]


def test_reasoning_loop_uses_tools_and_audits_result():
    agent, factory = make_agent([
        decision(tool="search_mail", arguments={"query": "from:zydus"}),
        decision(kind="final", answer="Zydus asked you to confirm payment."),
    ])
    result = agent.run("What should I know about Zydus payment mail?", "phone-1")
    assert result["steps"] == 2
    assert "confirm payment" in result["answer"]
    with factory() as db:
        audits = db.scalars(select(AgentAudit).order_by(AgentAudit.step)).all()
        assert [row.event for row in audits] == ["TOOL", "FINAL"]
    assert audits[0].tool_name == "search_mail"


def test_follow_up_turn_reuses_the_same_session_context():
    prompts = []

    class ContextLLM:
        def __init__(self):
            self.calls = 0

        def generate_json(self, _system, prompt, _schema):
            prompts.append(json.loads(prompt))
            self.calls += 1
            return decision(kind="final", answer="The newest one is from Zydus. What detail should I read?")

    agent, _ = make_agent([])
    agent.llm = ContextLLM()
    agent.run("Which meeting mail is newest?", "continuous-session")
    agent.run("Read the details", "continuous-session")

    assert prompts[1]["recent_conversation"]
    assert prompts[1]["recent_conversation"][-1]["user"] == "Which meeting mail is newest?"


def test_recent_mail_question_uses_the_agent_search_loop():
    agent, _ = make_agent([
        decision(tool="search_mail", arguments={"query": "Zydus"}),
        decision(kind="final", answer="The recent Zydus email asks you to confirm payment."),
    ])

    result = agent.run("Any recent update from Zydus?", "recent-mail")

    assert result["answer"] == "The recent Zydus email asks you to confirm payment."
    assert result["evidence"][0]["message_id"] == "m1"
    assert agent.gmail.queries == ["Zydus"]


def test_follow_up_can_reload_saved_thread_without_prior_tool_audits():
    agent, factory = make_agent([
        decision(tool="search_mail", arguments={"query": "meeting"}),
        decision(kind="final", answer="I found your meeting email."),
    ])
    agent.run("Find my meeting email", "saved-thread-context")
    with factory() as db:
        db.query(AgentAudit).delete()
        db.commit()

    prompts = []

    class FollowUpLLM:
        calls = 0

        def generate_json(self, _system, prompt, schema):
            payload = json.loads(prompt)
            if "matches" in schema.get("properties", {}):
                return {"requested_scope": "operational", "matches": [{
                    "message_id": item["message_id"], "excerpt": item["subject"],
                    "relationship": "operational", "reason": "The message is the active conversation."}
                    for item in payload["messages"]]}
            prompts.append(payload)
            self.calls += 1
            if self.calls == 1:
                return decision(tool="get_thread", arguments={"thread_id": payload["active_thread_id"]})
            return decision(kind="final", answer="They suggested meeting at 3 pm.")

    agent.llm = FollowUpLLM()
    result = agent.run("What time did they suggest?", "saved-thread-context")

    assert prompts[0]["active_thread_id"] == "thread-1"
    assert result["answer"] == "They suggested meeting at 3 pm."
    assert [item["message_id"] for item in result["evidence"]] == ["m1"]
    assert agent.gmail.queries == ["meeting"]


def test_conversation_state_selects_thread_when_evidence_has_multiple_messages_in_one_thread():
    agent, factory = make_agent([])
    evidence = {
        "first": {"message_id": "first", "thread_id": "shared-thread"},
        "second": {"message_id": "second", "thread_id": "shared-thread"},
    }

    with factory() as db:
        agent._save_conversation_state(db, "same-thread", evidence)
        state = db.query(ConversationState).filter_by(session_key="same-thread").one()

    assert state.selected_thread_id == "shared-thread"


def test_m3_keeps_bounded_recent_topics_and_returns_to_verified_thread():
    agent, factory = make_agent([])
    with factory() as db:
        for index in range(7):
            agent._save_conversation_state(db, "m3-topics", {
                f"m{index}": {"message_id": f"m{index}", "thread_id": f"t{index}",
                              "sender": f"Sender {index}", "subject": f"Topic {index}"}
            })
        db.commit()
        state = db.query(ConversationState).filter_by(session_key="m3-topics").one()
        assert [row["thread_id"] for row in state.topics_json] == ["t6", "t5", "t4", "t3", "t2"]
        topic, error = agent._resolve_topic_reference("go back to Sender 4", state)
        assert error is None and topic["thread_id"] == "t4"
        topic, error = agent._resolve_topic_reference("Sender 5 par wapas jao", state)
        assert error is None and topic["thread_id"] == "t5"
        topic, error = agent._resolve_topic_reference("वापस Sender 3", state)
        assert error is None and topic["thread_id"] == "t3"


def test_m3_ambiguous_recent_topic_asks_instead_of_guessing():
    state = ConversationState(topics_json=[
        {"message_id": "a", "thread_id": "ta", "sender": "ABC", "subject": "Quote"},
        {"message_id": "b", "thread_id": "tb", "sender": "ABC", "subject": "Delivery"},
    ], expires_at=datetime.utcnow() + timedelta(hours=1))
    topic, error = MailAgent._resolve_topic_reference("go back to ABC", state)
    assert topic is None
    assert "Quote" in error and "Delivery" in error and error.endswith("Which one do you mean?")


def test_m3_contextual_followup_revalidates_active_gmail_thread():
    class ContextGmail(FakeGmail):
        def __init__(self):
            super().__init__()
            self.threads = []
        def get_thread(self, thread_id, limit):
            self.threads.append(thread_id)
            return [{"sender": "ABC <abc@example.com>", "message_id": "abc-1",
                     "rfc_message_id": "<abc-1@example.com>", "subject": "Delivery",
                     "thread_id": thread_id, "received_at": datetime.now(timezone.utc).isoformat(),
                     "body": "We will deliver on Friday."}]

    agent, factory = make_agent([decision(kind="final", answer="They will deliver on Friday.")])
    agent.gmail = ContextGmail()
    with factory() as db:
        agent._save_conversation_state(db, "m3-followup", {
            "abc-1": {"message_id": "abc-1", "thread_id": "abc-thread",
                      "sender": "ABC", "subject": "Delivery"}})
        db.commit()
    result = agent.run("When will they deliver?", "m3-followup")
    assert result["answer"] == "They will deliver on Friday."
    assert result["evidence"][0]["thread_id"] == "abc-thread"
    assert agent.gmail.threads == ["abc-thread"]


def test_m3_deleted_active_source_is_not_answered_from_memory():
    class MissingGmail(FakeGmail):
        def get_thread(self, thread_id, limit):
            return []

    agent, factory = make_agent([AssertionError("model must not answer without Gmail evidence")])
    agent.gmail = MissingGmail()
    with factory() as db:
        agent._save_conversation_state(db, "m3-deleted", {
            "gone": {"message_id": "gone", "thread_id": "gone-thread",
                     "sender": "ABC", "subject": "Old update"}})
        db.commit()
    result = agent.run("What did they say?", "m3-deleted")
    assert "can no longer verify" in result["answer"]
    assert result["evidence"] == []


def test_m3_expired_state_is_cleaned_and_not_reused():
    agent, factory = make_agent([decision(kind="final", answer="I need a sender or subject.")])
    with factory() as db:
        db.add(ConversationState(mailbox_id="default", session_key="m3-expired",
                                 result_ids_json=["old"], topics_json=[{
                                     "message_id": "old", "thread_id": "old-thread",
                                     "sender": "Old", "subject": "Expired"}],
                                 selected_message_id="old", selected_thread_id="old-thread",
                                 expires_at=datetime.utcnow() - timedelta(seconds=1)))
        db.commit()
    result = agent.run("What did they say?", "m3-expired")
    assert result["answer"]
    with factory() as db:
        assert db.scalar(select(ConversationState).where(
            ConversationState.session_key == "m3-expired")) is None


def test_m3_draft_selection_is_thread_aware_after_topic_switch():
    first = AgentAction(id=1, action_type="PREPARE_DRAFT", status="PREPARED",
                        arguments_json={"thread_id": "thread-a", "subject": "A"})
    second = AgentAction(id=2, action_type="PREPARE_DRAFT", status="PREPARED",
                         arguments_json={"thread_id": "thread-b", "subject": "B"})
    selected, error = MailAgent._select_drafts(
        "make that reply more professional", [second, first], "thread-a")
    assert error is None and selected[0].id == 1
    _, error = MailAgent._select_drafts("send it now", [second, first], None)
    assert "multiple pending drafts" in error


def test_m3_conversation_state_isolated_by_mailbox_and_session():
    agent, factory = make_agent([])
    with factory() as db:
        agent._save_conversation_state(db, "shared-session", {
            "mine": {"message_id": "mine", "thread_id": "mine-thread",
                     "sender": "Mine", "subject": "Private"}}, "mailbox-a")
        agent._save_conversation_state(db, "shared-session", {
            "other": {"message_id": "other", "thread_id": "other-thread",
                      "sender": "Other", "subject": "Private"}}, "mailbox-b")
        db.commit()
        states = db.scalars(select(ConversationState).where(
            ConversationState.session_key == "shared-session")).all()
    assert {(row.mailbox_id, row.selected_thread_id) for row in states} == {
        ("mailbox-a", "mine-thread"), ("mailbox-b", "other-thread")}


def test_reply_to_that_mail_reuses_verified_mail_context():
    prompts = []

    class ReplyContextLLM:
        def __init__(self):
            self.calls = 0

        def generate_json(self, _system, prompt, _schema):
            payload = json.loads(prompt)
            prompts.append(payload)
            self.calls += 1
            if self.calls == 1:
                return decision(tool="search_mail", arguments={"query": "meeting"})
            return decision(kind="final", answer="Which reply should I send: acknowledge it or provide an update?",
                            rationale="The source mail is known, but the reply intent is missing.")

    agent, _ = make_agent([])
    agent.llm = ReplyContextLLM()
    agent.run("Find my meeting mail", "reply-context")
    result = agent.run("Reply to that mail", "reply-context")

    assert result["answer"]
    assert prompts[-1]["recent_conversation"]
    assert prompts[-1]["recent_conversation"][0]["user"] == "Find my meeting mail"


def test_sender_search_uses_model_query_and_source_evidence():
    agent, factory = make_agent([
        decision(tool="search_mail", arguments={"query": "from:client@example.com"}),
        decision(kind="final", answer="Payment: Please confirm."),
    ])
    result = agent.run("Any mail from that client?", "sender-search")
    assert result["evidence"][0]["message_id"] == "m1"
    assert agent.gmail.queries == ["from:client@example.com"]


def test_identical_gmail_search_page_is_not_repeated():
    agent, _ = make_agent([
        decision(tool="search_mail", arguments={"query": "from:client@example.com"}),
        decision(tool="search_mail", arguments={"query": "from:client@example.com"}),
        decision(kind="final", answer="I found the customer message."),
    ])
    result = agent.run("Find the customer's message", "dedupe-search")
    assert result["steps"] == 3
    assert result["evidence"][0]["message_id"] == "m1"
    assert agent.gmail.queries == ["from:client@example.com"]


def test_spoken_answer_never_ends_mid_word_or_mid_date():
    answer = ("I found 8 matching emails related to Zydus:\n\n1 direct correspondence:\n"
              "- From Dishan: Zydus support issue (received Sep 16, 2026).\n"
              "7 newsletter mentions from Groww Digest.")
    spoken = MailAgent._spoken(answer)
    assert len(spoken.split()) <= 25
    assert spoken.endswith('.')  # Stop at a complete sentence, never mid-date or mid-word.


def test_detailed_answer_has_a_separate_short_spoken_summary():
    full_answer = 'Zydus asked you to confirm payment. ' + 'The source email contains additional details for your review. ' * 4
    final = decision(kind='final', answer=full_answer)
    final['spoken_answer'] = 'Zydus asked you to confirm payment.'
    agent, _ = make_agent([decision(tool='search_mail', arguments={'query': 'Zydus payment'}), final])
    result = agent.run('Tell me about the Zydus payment email', 'voice-summary')
    assert result['answer'] == full_answer.strip()
    assert result['spoken_answer'] == 'Zydus asked you to confirm payment.'


def test_agent_flags_a_generated_total_that_disagrees_with_verified_evidence():
    agent, _ = make_agent([
        decision(tool="search_mail", arguments={"query": "from:client@example.com"}),
        decision(kind="final", answer="I found 2 emails from the client."),
    ])
    result = agent.run("Find the client's email", "count-mismatch")
    assert "did not match the 1 verified source message" in result["answer"]
    assert "1 from Zydus." in result["answer"]
    assert result["evidence"][0]["message_id"] == "m1"


def test_agent_flags_wrong_sender_group_count_even_when_total_is_correct():
    evidence = {"direct": {"sender": "Dishan"}}
    evidence.update({f"digest-{index}": {"sender": "Groww Digest"} for index in range(8)})
    answer = ("I found 9 emails: 1 direct correspondence from Dishan and "
              "9 mentions in Groww Digest newsletters.")
    assert MailAgent._count_mismatch(answer, evidence)


def test_gmail_search_also_checks_synced_history_and_keeps_sender_provenance():
    agent, factory = make_agent([
        decision(tool="search_mail", arguments={"query": "Zydus"}),
        decision(kind="final", answer="One support email from you, plus one Groww Digest mention."),
    ])
    with factory() as db:
        email = Email(provider="gmail", provider_message_id="support-1", thread_id="support-thread",
                     sender_name="Dishan", sender_email="dishan@example.test",
                     subject="Zydus support issue — device error", received_at=datetime(2026, 9, 16),
                     body_hash="hash")
        db.add(email)
        db.flush()
        db.add(EmailAnalysis(email_id=email.id, importance="MEDIUM", category="SUPPORT",
                             needs_reply=True, needs_action=True, urgency="NORMAL", sender="Dishan",
                             subject=email.subject, summary="Reports a Zydus attendance device error.",
                             why_important="Support is requested.", requested_action="Investigate the issue.",
                             deadline=None, key_facts_json=[], risk_flags_json=[], confidence=0.9,
                             model="test", prompt_version="test"))
        db.commit()
    queries = []
    def search(query, limit):
        queries.append(query)
        return [{"message_id": "digest-1", "thread_id": "digest-thread",
                 "sender": "Groww Digest", "subject": "Daily market digest",
                 "received_at": "2026-08-26T16:35:33+00:00",
                 "body": "Zydus Lifesciences announced a subsidiary."}]
    agent.gmail.search = search

    result = agent.run("zydus related all mail", "zydus-history")

    assert {item["message_id"] for item in result["evidence"]} == {"support-1", "digest-1"}
    support = next(item for item in result["evidence"] if item["message_id"] == "support-1")
    digest = next(item for item in result["evidence"] if item["message_id"] == "digest-1")
    assert support["retrieval_source"] == ["local history"]
    assert digest["retrieval_source"] == ["Gmail"]
    assert queries == ["Zydus"]


def test_unanalyzed_body_only_mail_is_retrievable_from_fts() -> None:
    agent, factory = make_agent([])
    with factory() as db:
        db.execute(text("""CREATE VIRTUAL TABLE message_search USING fts5(
            provider UNINDEXED, provider_message_id UNINDEXED,
            sender_name, sender_email, subject, body_text, summary
        )"""))
        db.add(Email(provider="gmail", provider_message_id="pending-1",
                     thread_id="thread-1", sender_name="Operator",
                     sender_email="operator@example.test", subject="Device support",
                     received_at=datetime(2026, 9, 16), body_hash="hash"))
        db.execute(text("""INSERT INTO message_search
            (provider, provider_message_id, sender_name, sender_email, subject, body_text, summary)
            VALUES ('gmail', 'pending-1', 'Operator', 'operator@example.test',
                    'Device support', 'The attendance device stopped recording punches.', '')"""))
        db.commit()

        result = agent._search_history(db, {"text": "attendance"})

    assert len(result["messages"]) == 1
    assert result["messages"][0]["message_id"] == "pending-1"
    assert result["messages"][0]["analysis_status"] == "pending"
    assert "attendance device" in result["messages"][0]["body"]


def test_structured_history_filters_apply_date_labels_and_attachments() -> None:
    agent, factory = make_agent([])
    with factory() as db:
        common = dict(provider="gmail", mailbox_id="default", thread_id="t",
                      sender_name="Priya", sender_email="priya@example.test",
                      body_hash="x", is_deleted=False)
        today = Email(provider_message_id="today", subject="Today", received_at=datetime.now(),
                      labels_json=["INBOX", "UNREAD"], **common)
        old = Email(provider_message_id="old", subject="Old", received_at=datetime.now() - timedelta(days=2),
                    labels_json=["SENT"], **common)
        db.add_all([today, old]); db.flush()
        db.add(Attachment(email_id=today.id, filename="report.pdf", mime_type="application/pdf"))
        db.commit()
        result = agent._search_history(db, {"unread": True,
                                             "inbox": True, "has_attachments": True})
    assert [row["message_id"] for row in result["messages"]] == ["today"]


def test_structured_history_sent_filter_excludes_inbox_mail() -> None:
    agent, factory = make_agent([])
    with factory() as db:
        for ident, labels in (("sent", ["SENT"]), ("inbox", ["INBOX"])):
            db.add(Email(provider="gmail", mailbox_id="default", provider_message_id=ident,
                         thread_id=ident, subject=ident, sender_name="A", sender_email="a@test",
                         received_at=datetime.now(), body_hash=ident, labels_json=labels))
        db.commit()
        result = agent._search_history(db, {"sent": True})
    assert [row["message_id"] for row in result["messages"]] == ["sent"]


def test_broad_company_search_excludes_third_party_mentions_by_default():
    agent, _ = make_agent([])
    class RelevanceLLM:
        decisions = iter([
            decision(tool="search_mail", arguments={"query": "Acme"}),
            decision(kind="final", answer="I found one operational Acme email."),
        ])
        def generate_json(self, _system, prompt, schema):
            if "matches" not in schema.get("properties", {}):
                return next(self.decisions)
            messages = json.loads(prompt)["messages"]
            # Even a bad model scope must not turn "all mail" into newsletter permission.
            return {"requested_scope": "all", "matches": [
                {"message_id": item["message_id"], "excerpt": item["body"],
                 "relationship": ("operational" if item["message_id"] == "support" else "third_party_mention"),
                 "reason": ("A support request about Acme." if item["message_id"] == "support"
                            else "An unrelated publisher mentions Acme.")}
                for item in messages]}
    agent.llm = RelevanceLLM()
    agent.gmail.search = lambda *_: [
        {"message_id": "support", "thread_id": "support-thread", "sender": "Technician",
         "subject": "Device problem", "body": "The Acme attendance device reports error 42."},
        {"message_id": "digest", "thread_id": "digest-thread", "sender": "Market Digest",
         "subject": "Daily markets", "body": "Acme shares rose in afternoon trading."},
    ]

    result = agent.run("Show all Acme related mail", "company-relevance")

    assert [item["message_id"] for item in result["evidence"]] == ["support"]
    assert result["evidence"][0]["relationship"] == "operational"


def test_explicit_news_request_returns_third_party_mentions():
    agent, _ = make_agent([])
    class RelevanceLLM:
        decisions = iter([
            decision(tool="search_mail", arguments={"query": "Acme news"}),
            decision(kind="final", answer="I found one news mention of Acme."),
        ])
        def generate_json(self, _system, prompt, schema):
            if "matches" not in schema.get("properties", {}):
                return next(self.decisions)
            item = json.loads(prompt)["messages"][0]
            return {"requested_scope": "third_party", "matches": [{
                "message_id": item["message_id"], "excerpt": item["body"],
                "relationship": "third_party_mention", "reason": "A publisher reports on Acme."}]}
    agent.llm = RelevanceLLM()
    agent.gmail.search = lambda *_: [{"message_id": "news", "thread_id": "news-thread",
        "sender": "Industry Journal", "subject": "Sector update", "body": "Acme opened a new plant."}]

    result = agent.run("Show news mentions about Acme", "company-news")

    assert [item["message_id"] for item in result["evidence"]] == ["news"]
    assert result["evidence"][0]["relationship"] == "third_party_mention"


def test_fresh_local_mail_is_searchable_without_restart_or_fixture_keyword():
    agent, factory = make_agent([
        decision(tool="search_mail", arguments={"query": "Quasar Loom maintenance"}),
        decision(kind="final", answer="I found the maintenance request."),
    ])
    agent.gmail.search = lambda *_: []
    with factory() as db:
        email = Email(provider="gmail", provider_message_id="fresh-quasar", thread_id="fresh-thread",
                      sender_name="Plant engineer", sender_email="engineer@example.test",
                      subject="Loom maintenance", received_at=datetime.now(timezone.utc), body_hash="fresh")
        db.add(email)
        db.flush()
        db.add(EmailAnalysis(email_id=email.id, importance="MEDIUM", category="SUPPORT",
                            needs_reply=True, needs_action=True, urgency="NORMAL", sender="Plant engineer",
                            subject=email.subject, summary="Quasar Loom requires bearing maintenance.",
                            why_important="Production maintenance is requested.", requested_action="Inspect bearing.",
                            deadline=None, key_facts_json=[], risk_flags_json=[], confidence=0.9,
                            model="test", prompt_version="test"))
        db.commit()

    result = agent.run("Find Quasar Loom maintenance mail", "fresh-data")

    assert [item["message_id"] for item in result["evidence"]] == ["fresh-quasar"]


def test_history_uses_one_latest_analysis_per_message_and_the_current_provider():
    agent, factory = make_agent([])
    with factory() as db:
        for provider, summaries in [("gmail", ["Obsolete analysis", "Current analysis"]),
                                    ("demo", ["Demo analysis"])]:
            email = Email(provider=provider, provider_message_id="repeated-id", thread_id="thread",
                          sender_name="Helix", sender_email="operator@helix.example",
                          subject="Inspection", received_at=datetime.now(timezone.utc), body_hash="test")
            db.add(email)
            db.flush()
            for summary in summaries:
                db.add(EmailAnalysis(email_id=email.id, importance="MEDIUM", category="SUPPORT",
                                    needs_reply=False, needs_action=False, urgency="NORMAL", sender="Helix",
                                    subject="Inspection", summary=summary, confidence=0.9,
                                    model="test", prompt_version="test"))
            db.flush()
        db.commit()
        result = agent._search_history(db, {"text": "operator@helix.example"})
    assert len(result["messages"]) == 1
    assert result["messages"][0]["summary"] == "Current analysis"


def test_garbage_no_result_query_returns_honest_empty_state_without_crashing():
    agent, _ = make_agent([
        decision(tool="search_mail", arguments={"query": "🧪 ../../etc/passwd"}),
        decision(kind="final", answer="No result."),
        decision(tool="search_mail", arguments={"query": '"🧪"'}),
        decision(kind="final", answer="No result."),
    ])
    agent.gmail.search = lambda *_: []

    result = agent.run("🧪 ../../etc/passwd", "garbage-query")

    assert result["evidence"] == []
    assert "couldn't find mail matching your request" in result["answer"]


def test_selected_reply_thread_is_loaded_without_a_broad_search():
    agent, _ = make_agent([
        decision(tool="prepare_draft", arguments={"to": "client@example.com", "subject": "Re: Meeting",
                 "body": "I am not free at 3 pm. Can we meet at 5 pm?", "thread_id": "thread-1"}),
        decision(kind="final", answer="Draft ready."),
    ])
    result = agent.run("Please ask to move the meeting from 3 pm to 5 pm.", "selected-reply",
                       seed_message_id="m1", seed_thread_id="thread-1")
    assert result["state"] == "READ_BACK" and result["draft"]["thread_id"] == "thread-1"
    assert agent.gmail.queries == []


def test_selected_reply_accepts_verified_display_name_address():
    agent, _ = make_agent([
        decision(tool="prepare_draft", arguments={"to": "Zydus <client@example.com>",
                 "subject": "Re: Meeting", "body": "Can we meet at 5 pm?", "thread_id": "thread-1"}),
        decision(kind="final", answer="Draft ready."),
    ])
    result = agent.run("Please ask to move the meeting to 5 pm.", "display-address",
                       seed_message_id="m1", seed_thread_id="thread-1")
    assert result["draft"]["to"] == "client@example.com"


def test_prepared_reply_survives_second_gemini_call_failure():
    agent, _ = make_agent([
        decision(tool="prepare_draft", arguments={"to": "client@example.com", "subject": "Re: Meeting",
                 "body": "Can we meet at 5 pm?", "thread_id": "thread-1"}),
        LLMProviderError("temporary 503"),
    ])
    result = agent.run("Please ask to move the meeting to 5 pm.", "reply-recovery",
                       seed_message_id="m1", seed_thread_id="thread-1")
    assert result["state"] == "READ_BACK" and result["draft"]["body"] == "Can we meet at 5 pm?"


def test_empty_mail_search_is_rejected_instead_of_searching_entire_inbox():
    agent, _ = make_agent([
        decision(tool="search_mail"),
        decision(kind="final", answer="I need a more specific search."),
    ])
    result = agent.run("Find a message", "phone-empty")
    assert "specific" in result["answer"]


def test_memory_and_risky_action_are_durable_and_approval_gated():
    agent, factory = make_agent([
        decision(tool="remember", arguments={"kind": "preference", "key": "briefing",
                 "value": "short", "source": "user said so", "confidence": .9}),
        decision(tool="propose_risky_action", arguments={"action_type": "SEND",
                 "arguments": {"to": "a@example.com"}, "rationale": "User requested a reply"}),
        decision(kind="final", answer="I prepared a send proposal and need your approval."),
    ])
    agent.run("Remember short briefs and send a reply", "phone-2")
    with factory() as db:
        memory = db.scalar(select(AgentMemory))
        action = db.scalar(select(AgentAction))
        assert memory.value == "short"
        assert action.status == "PENDING_APPROVAL"
        assert action.risk == "HIGH"


def test_model_cannot_self_approve_without_explicit_user_confirmation():
    agent, factory = make_agent([
        decision(tool="propose_risky_action", arguments={"action_type": "DELETE", "arguments": {}}),
        decision(tool="approve_action", arguments={"action_id": 1}),
        decision(kind="final", answer="Approval is still required."),
    ])
    agent.run("Clean this up", "phone-3")
    with factory() as db:
        assert db.get(AgentAction, 1).status == "PENDING_APPROVAL"


def test_draft_is_prepared_locally_without_sending_mail():
    agent, factory = make_agent([
        decision(tool="prepare_draft", arguments={"to": "client@example.com",
                 "subject": "Payment", "body": "Thanks, I will confirm tomorrow."}),
        decision(kind="final", answer="The draft is prepared."),
    ])
    agent.run("Draft to client@example.com saying I will confirm tomorrow", "phone-draft")
    with factory() as db:
        action = db.scalar(select(AgentAction))
        assert action.action_type == "PREPARE_DRAFT"
        assert action.status == "PREPARED"
        assert action.arguments_json["to"] == "client@example.com"


def test_done_sends_the_reviewed_draft_once():
    agent, factory = make_agent([
        decision(tool="search_mail", arguments={"query": "from:client@example.com"}),
        decision(tool="prepare_draft", arguments={"to": "client@example.com", "subject": "Update",
                 "body": "We need the device model and error code.", "thread_id": "thread-1",
                 "in_reply_to": "<message-1@example.com>"}),
        decision(kind="final", answer="Draft ready."),
    ])
    first = agent.run("Reply to the customer saying we need the device model and error code", "phone-send")
    assert first["draft"]["to"] == "client@example.com"
    blocked = agent.run("done", "phone-send")
    assert blocked["state"] == "READ_BACK"
    assert not agent.gmail.sent
    with factory() as db:
        review = begin_review(db, first["draft"]["id"], "phone-send")
        complete_review(db, first["draft"]["id"], "phone-send", review["review_ticket"])
    sent = agent.run("done", "phone-send", review_ticket=review["review_ticket"])
    assert sent["answer"] == "Sent to client@example.com."
    assert len(agent.gmail.sent) == 1
    assert agent.gmail.sent[0]["thread_id"] == "thread-1"
    again = agent.run("done", "phone-send")
    assert "draft" in again["answer"].lower() or "review" in again["answer"].lower()
    assert len(agent.gmail.sent) == 1


def test_model_invented_recipient_cannot_become_a_draft():
    agent, factory = make_agent([
        decision(tool="search_mail", arguments={"query": "Zydus"}),
        decision(tool="prepare_draft", arguments={"to": "wrong@example.com", "subject": "Reply",
                 "body": "Thanks", "thread_id": "thread-1"}),
        decision(kind="final", answer="Which recipient should receive this?"),
    ])
    result = agent.run("Reply to Zydus", "safe")
    assert not result["draft"]
    with factory() as db:
        assert db.scalar(select(AgentAction)) is None


def test_review_expiry_and_low_confidence_block_send():
    from app.database.models import DraftReview
    agent, factory = make_agent([
        decision(tool="prepare_draft", arguments={"to": "client@example.com", "subject": "Hello", "body": "Thank you."}),
        decision(kind="final", answer="Ready"),
    ])
    result = agent.run("Write thank you to client@example.com", "safe")
    with factory() as db:
        review = begin_review(db, result["draft"]["id"], "safe")
        complete_review(db, result["draft"]["id"], "safe", review["review_ticket"])
    assert agent.run("done", "safe", review_ticket=review["review_ticket"], confidence_tier="MEDIUM")["state"] == "READ_BACK"
    assert agent.run("done", "another", review_ticket=review["review_ticket"])["state"] == "READ_BACK"
    with factory() as db:
        db.get(DraftReview, result["draft"]["id"]).completed_at -= timedelta(seconds=61)
        db.commit()
    assert agent.run("done", "safe", review_ticket=review["review_ticket"])["state"] == "READ_BACK"
    assert not agent.gmail.sent


def test_new_request_invalidates_previous_exact_readback():
    agent, factory = make_agent([
        decision(tool="prepare_draft", arguments={"to": "client@example.com", "subject": "Hello", "body": "Thank you."}),
        decision(kind="final", answer="Ready"),
        decision(kind="final", answer="I have no matching mail."),
    ])
    result = agent.run("Write thank you to client@example.com", "invalidate")
    with factory() as db:
        review = begin_review(db, result["draft"]["id"], "invalidate")
        complete_review(db, result["draft"]["id"], "invalidate", review["review_ticket"])
    agent.run("How many invoice mails?", "invalidate")
    assert agent.run("done", "invalidate", review_ticket=review["review_ticket"])["state"] == "READ_BACK"
    assert not agent.gmail.sent


def test_model_outage_returns_safe_degraded_result_without_action():
    class FailingLLM:
        def generate_json(self, *_args):
            raise LLMProviderError("private provider body")

    agent, factory = make_agent([])
    agent.llm = FailingLLM()
    result = agent.run("Draft a reply", "outage")

    assert result["state"] == "DEGRADED"
    assert "private" not in result["answer"]
    with factory() as db:
        assert db.scalar(select(AgentAction)) is None
        assert db.scalar(select(AgentAudit)).event == "MODEL_UNAVAILABLE"


def test_failed_gmail_sync_with_no_evidence_skips_gemini(monkeypatch):
    class FailIfCalled:
        def generate_json(self, *_args):
            raise AssertionError('Gemini must not answer without available mail')

    agent, factory = make_agent([])
    agent.gmail = None
    agent.llm = FailIfCalled()
    from app.api import dependencies
    monkeypatch.setattr(dependencies, 'create_generation_service',
                        lambda: (_ for _ in ()).throw(ConnectionError('synthetic Gmail outage')))

    result = agent.run('Who emailed me today?', 'gmail-outage')

    assert result['state'] == 'DEGRADED'
    assert 'Gmail connection' in result['answer']
    with factory() as db:
        assert db.scalar(select(AgentAudit)).event == 'MAIL_UNAVAILABLE'


def test_draft_cannot_invent_business_status():
    agent, factory = make_agent([
        decision(tool="search_mail", arguments={"query": "from:client@example.com"}),
        decision(tool="prepare_draft", arguments={"to": "client@example.com", "subject": "Update",
                 "body": "The attendance issue is resolved.", "thread_id": "thread-1"}),
        decision(kind="final", answer="Please confirm whether the issue is resolved."),
    ])
    result = agent.run("Reply to the customer with an update", "invented-status")

    assert result["draft"] is None
    with factory() as db:
        assert db.scalar(select(AgentAction)) is None


def test_contextual_reply_can_be_revised_without_losing_thread_or_recipient():
    agent, factory = make_agent([
        decision(tool="search_mail", arguments={"query": "from:client@example.com"}),
        decision(kind="final", answer="I found the payment email."),
        decision(tool="prepare_draft", arguments={"to": "client@example.com", "subject": "Re: Payment",
                 "body": "We will deliver by Friday.", "thread_id": "thread-1",
                 "in_reply_to": "<message-1@example.com>", "references": "<root@example.com>"}),
        decision(kind="final", answer="Draft ready."),
        decision(tool="prepare_draft", arguments={"body": "We will deliver by Friday as discussed. Thank you."}),
        decision(kind="final", answer="I made the reply more professional."),
    ])
    agent.run("Find the email from client@example.com", "revision")
    first = agent.run("Reply to that email and tell them we will deliver by Friday", "revision")
    with factory() as db:
        old_review = begin_review(db, first["draft"]["id"], "revision")
        complete_review(db, first["draft"]["id"], "revision", old_review["review_ticket"])
    revised = agent.run("Make it more professional and shorter", "revision")
    assert revised["draft"]["body"] == "We will deliver by Friday as discussed. Thank you."
    assert revised["draft"]["to"] == first["draft"]["to"]
    assert revised["draft"]["subject"] == first["draft"]["subject"]
    assert revised["draft"]["thread_id"] == first["draft"]["thread_id"]
    assert revised["draft"]["in_reply_to"] == first["draft"]["in_reply_to"]
    assert revised["draft"]["references"] == first["draft"]["references"]
    with factory() as db:
        actions = db.scalars(select(AgentAction).order_by(AgentAction.id)).all()
        assert [row.status for row in actions] == ["SUPERSEDED", "PREPARED"]
    blocked = agent.run("done", "revision", review_ticket=old_review["review_ticket"])
    assert blocked["state"] == "READ_BACK"
    assert not agent.gmail.sent


def test_send_now_returns_exact_readback_and_never_sends():
    agent, _ = make_agent([
        decision(tool="prepare_draft", arguments={"to": "client@example.com", "subject": "Reply",
                 "body": "Thank you."}),
        decision(kind="final", answer="Ready."),
    ])
    prepared = agent.run("Write thank you to client@example.com", "send-now")
    result = agent.run("send now", "send-now")
    assert result["state"] == "READ_BACK"
    assert "To: client@example.com\nSubject: Reply\n\nThank you." in result["answer"]
    assert not agent.gmail.sent
    assert prepared["draft"]["id"] == result["draft"]["id"]


def test_revision_cannot_change_recipient_outside_verified_thread():
    agent, factory = make_agent([
        decision(tool="search_mail", arguments={"query": "from:client@example.com"}),
        decision(kind="final", answer="I found the email."),
        decision(tool="prepare_draft", arguments={"to": "client@example.com", "subject": "Reply",
                 "body": "We will deliver Friday.", "thread_id": "thread-1"}),
        decision(kind="final", answer="Ready."),
        decision(tool="prepare_draft", arguments={"to": "attacker@example.test", "body": "Thanks."}),
        decision(kind="final", answer="I could not revise to that recipient."),
    ])
    agent.run("Find the email from client@example.com", "wrong-revision")
    agent.run("Reply to that email; we will deliver Friday", "wrong-revision")
    result = agent.run("Change recipient to attacker@example.test", "wrong-revision")
    assert result["draft"]["to"] == "client@example.com"
    with factory() as db:
        action = db.scalar(select(AgentAction))
        assert action.status == "PREPARED"
    assert not agent.gmail.sent


def test_hinglish_send_now_is_review_only():
    agent, _ = make_agent([
        decision(tool="prepare_draft", arguments={"to": "client@example.com", "subject": "Reply",
                 "body": "Thank you."}),
        decision(kind="final", answer="Ready."),
    ])
    agent.run("Write thank you to client@example.com", "hinglish-send")
    result = agent.run("abhi bhej do", "hinglish-send")
    assert result["state"] == "READ_BACK"
    assert not agent.gmail.sent


def test_gmail_failure_is_unknown_and_never_retried():
    agent, factory = make_agent([
        decision(tool="prepare_draft", arguments={"to": "client@example.com", "subject": "Reply",
                 "body": "Thank you."}),
        decision(kind="final", answer="Ready."),
    ])
    draft = agent.run("Write thank you to client@example.com", "provider-failure")["draft"]
    with factory() as db:
        review = begin_review(db, draft["id"], "provider-failure")
        complete_review(db, draft["id"], "provider-failure", review["review_ticket"])
    attempts = []
    def fail_send(_draft):
        attempts.append(True)
        raise ConnectionError("synthetic provider failure")
    agent.gmail.send_message = fail_send
    result = agent.run("done", "provider-failure", review_ticket=review["review_ticket"])
    assert result["state"] == "SEND_UNCONFIRMED"
    assert "Check Gmail Sent" in result["answer"]
    again = agent.run("done", "provider-failure", review_ticket=review["review_ticket"])
    assert again["state"] == "READ_BACK"
    assert len(attempts) == 1
    with factory() as db:
        assert db.get(AgentAction, draft["id"]).status == "SEND_UNCONFIRMED"


def test_missing_send_authorization_keeps_draft_and_requires_fresh_review():
    from app.email.gmail import GmailConfigurationError
    from app.database.models import DraftReview
    agent, factory = make_agent([
        decision(tool="prepare_draft", arguments={"to": "client@example.com", "subject": "Reply",
                 "body": "Thank you."}),
        decision(kind="final", answer="Ready."),
    ])
    draft = agent.run("Write thank you to client@example.com", "missing-send-auth")["draft"]
    with factory() as db:
        review = begin_review(db, draft["id"], "missing-send-auth")
        complete_review(db, draft["id"], "missing-send-auth", review["review_ticket"])
    attempts = []
    def missing_send_auth(_draft):
        attempts.append(True)
        raise GmailConfigurationError("Separate Gmail send authorization is required.")
    agent.gmail.send_message = missing_send_auth
    result = agent.run("done", "missing-send-auth", review_ticket=review["review_ticket"])
    assert result["state"] == "READ_BACK"
    assert "Nothing was sent" in result["answer"]
    assert len(attempts) == 1
    with factory() as db:
        action = db.get(AgentAction, draft["id"])
        assert action.status == "PREPARED"
        assert db.get(DraftReview, draft["id"]).completed_at is None


def test_context_planner_requires_a_verified_source_without_phrase_matching():
    agent, factory = make_agent([
        decision(kind="final", answer="The sender is Zydus."),
        decision(tool="get_thread", arguments={"thread_id":"active-thread"}),
        decision(kind="final", answer="Zydus sent a meeting request."),
    ])
    with factory() as db:
        agent._save_conversation_state(db, "context-planner", {
            "m1":{"message_id":"m1","thread_id":"active-thread","sender":"Zydus","subject":"Meeting"}})
        db.commit()
    assert not agent._is_contextual_followup("Sender?")
    result=agent.run("Sender?", "context-planner")
    assert result["answer"]=="Zydus sent a meeting request."
    assert result["evidence"][0]["thread_id"]=="active-thread"
    assert result["steps"]==3


def test_context_feedback_cannot_be_used_as_mail_evidence():
    agent, factory = make_agent([decision(kind="final", answer="Invented mail fact.")]*3)
    with factory() as db:
        agent._save_conversation_state(db,"no-context-evidence",{
            "m1":{"message_id":"m1","thread_id":"active-thread","sender":"Zydus","subject":"Meeting"}})
        db.commit()
    result=agent.run("Sender?","no-context-evidence",max_steps=3)
    assert result["answer"]!="Invented mail fact."
    assert result["evidence"]==[]


def test_context_feedback_still_allows_a_specific_clarification():
    question="Which email should I check?"
    agent, factory = make_agent([
        decision(kind="final",answer="Unsupported claim."),
        {**decision(kind="final",answer=question),"pending_question":question},
    ])
    with factory() as db:
        agent._save_conversation_state(db,"context-clarification",{
            "m1":{"message_id":"m1","thread_id":"active-thread","sender":"Zydus","subject":"Meeting"}})
        db.commit()
    result=agent.run("Sender?","context-clarification")
    assert result["answer"]==question
    assert result["evidence"]==[]
