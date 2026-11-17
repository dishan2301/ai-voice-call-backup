import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi import HTTPException

from app.agents.whatsapp_rewrite import rewrite_message
from app.api.phone import WhatsAppRewriteRequest, whatsapp_rewrite


@pytest.mark.parametrize('instruction', ['make it professional', 'make it shorter', 'write it politely',
                                         'ise chhota karo', 'इसे विनम्र बनाओ'])
def test_rewrite_sends_only_current_message_and_instruction(instruction):
    class Provider:
        def generate_json(self, system, prompt, schema):
            assert json.loads(prompt) == {'message': 'please share the report', 'instruction': instruction}
            return {'message': 'Please share the report.'}
    assert rewrite_message('please share the report', instruction, None, Provider()) == 'Please share the report.'


@pytest.mark.parametrize('output', ['We will deliver tomorrow.', 'The price is 500.', 'Payment is approved.', ''])
def test_unsupported_facts_and_commitments_are_rejected(output):
    class Provider:
        def generate_json(self, *args):
            return {'message': output}
    with pytest.raises(ValueError):
        rewrite_message('Please share the report.', 'make it professional', None, Provider())


def test_endpoint_failure_has_no_send_or_draft_mutation():
    import asyncio
    original = WhatsAppRewriteRequest(message='Please share the report.', instruction='make it shorter')
    with patch('app.agents.whatsapp_rewrite.rewrite_message', side_effect=RuntimeError('provider failed')):
        with pytest.raises(HTTPException) as error:
            asyncio.run(whatsapp_rewrite(original, SimpleNamespace(mailbox_id='synthetic')))
    assert error.value.status_code == 503
    assert original.message == 'Please share the report.'


def test_endpoint_rejects_contact_book_or_recipient_payload():
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        WhatsAppRewriteRequest(message='Hello', instruction='polite', contacts=[{'number': 'private'}])
