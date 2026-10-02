from outlook_triage.filters import FilterRules, clean_body, skip_reason


def test_clean_body_removes_quoted_history_and_tracking_parameters():
    text = "Please submit today.\nhttps://example.com/form?user=secret#x\n\nOn Monday Alice wrote:\n> old mail"
    cleaned = clean_body(text)
    assert "Please submit today." in cleaned
    assert "https://example.com/form" in cleaned
    assert "secret" not in cleaned
    assert "old mail" not in cleaned


def test_clean_body_caps_text():
    assert clean_body("x" * 100, max_chars=12) == "x" * 12


def test_explicit_rules_only():
    rules = FilterRules(
        ignored_senders=frozenset({"news@example.com"}),
        ignored_domains=frozenset({"marketing.test"}),
        ignored_subject_patterns=(),
    )
    assert skip_reason("news@example.com", "Hello", rules) == "ignored sender"
    assert skip_reason("person@marketing.test", "Hello", rules) == "ignored domain"
    assert skip_reason("no-reply@important.test", "Action required", rules) is None

