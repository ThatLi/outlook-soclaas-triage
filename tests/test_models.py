import pytest
from pydantic import ValidationError

from outlook_triage.models import EmailClassification
from outlook_triage.soclaas import _extract_json


def test_valid_classification():
    value = EmailClassification.model_validate(
        {
            "requires_action": True,
            "urgency": "soon",
            "action_type": "reply",
            "task": "Reply to Alice",
            "deadline": "2026-09-30",
            "deadline_raw": "by Wednesday",
            "summary": "Alice requested a reply.",
            "category": "action",
            "reason": "The email asks a direct question.",
            "confidence": 0.9,
        }
    )
    assert value.deadline.isoformat() == "2026-09-30"


def test_non_action_must_use_none_urgency():
    with pytest.raises(ValidationError):
        EmailClassification.model_validate(
            {
                "requires_action": False,
                "urgency": "normal",
                "action_type": "none",
                "task": None,
                "deadline": None,
                "deadline_raw": None,
                "summary": "FYI",
                "category": "information",
                "reason": "No request.",
                "confidence": 0.8,
            }
        )


def test_extract_json_from_fence():
    assert _extract_json('```json\n{"ok": true}\n```') == {"ok": True}

