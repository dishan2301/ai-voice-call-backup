from app.services.receptionist import normalize_phone, normalize_allowlist, caller_allowed, ReceptionistSession

def test_indian_phone_normalization_and_ambiguous_ids():
    assert normalize_phone("09876543210") == "+919876543210"
    assert normalize_phone("+91 98765 43210") == "+919876543210"
    assert normalize_phone("withheld") is None
    assert normalize_allowlist(["9876543210", "+91 98765 43210"]) == ["+919876543210"]

def test_ai_mode_and_allowlist_are_strict():
    assert not caller_allowed(False, "9876543210", ["9876543210"])
    assert not caller_allowed(True, "9876543210", ["9123456789"])
    assert caller_allowed(True, "9876543210", ["+91 98765 43210"])

def test_receptionist_context_has_no_personal_tools():
    prompt = ReceptionistSession("+919876543210", "session-key-123456").prompt()
    assert "Gmail" in prompt and "Calendar" in prompt and "WhatsApp" in prompt
    assert "private messages" in prompt
