from app.agents.commitments import extract_commitments


def test_literal_promises_are_grounded_and_directional():
    incoming = extract_commitments(
        "Thanks. We will send the revised quotation by Friday. Please review it.", ["INBOX"])
    sent = extract_commitments("I will provide the error logs tomorrow.", ["SENT"])
    request = extract_commitments("Please send the invoice by Friday.", ["INBOX"])

    assert incoming == [{"direction": "THEY_OWE", "text": "We will send the revised quotation by Friday.",
                         "due_text": "by Friday"}]
    assert sent == [{"direction": "YOU_OWE", "text": "I will provide the error logs tomorrow.",
                     "due_text": "tomorrow"}]
    assert request == []


def test_due_capture_supports_plain_relative_dates_and_keeps_uncertain_dates_empty():
    explicit = extract_commitments("I will send the signed form 21st October 2026.", ["SENT"])
    absent = extract_commitments("I will send the signed form when legal approves it.", ["SENT"])

    assert explicit[0]["due_text"] == "21st October 2026"
    assert absent[0]["due_text"] is None
