"""Tests for injected Secret Service storage without a desktop D-Bus."""

from meeting_recorder.calendar_secrets import CalendarSecrets, SecretServiceError


class _Schema:
    created = None

    @staticmethod
    def new(name, flags, attributes):
        _Schema.created = (name, flags, attributes)
        return "schema"


class _Secret:
    Schema = _Schema

    class SchemaFlags:
        NONE = "none"

    class SchemaAttributeType:
        STRING = "string"

    COLLECTION_DEFAULT = "default"

    def __init__(self):
        self.token = None
        self.calls = []

    def password_lookup_sync(self, schema, attributes, cancellable):
        self.calls.append(("load", schema, attributes, cancellable))
        return self.token

    def password_store_sync(self, schema, attributes, collection, label, token, cancellable):
        self.calls.append(("save", schema, attributes, collection, label, token, cancellable))
        self.token = token

    def password_clear_sync(self, schema, attributes, cancellable):
        self.calls.append(("clear", schema, attributes, cancellable))
        self.token = None


def test_secret_service_uses_one_schema_account_and_raw_token_only():
    api = _Secret()
    store = CalendarSecrets(api)
    store.save("refresh-token")
    assert store.load() == "refresh-token"
    store.clear()
    assert store.load() is None
    assert _Schema.created == (
        "org.meeting_recorder.GoogleCalendar", "none", {"account": "string"})
    assert all(call[2] == {"account": "google-calendar-refresh-token"}
               for call in api.calls)
    assert api.calls[0][5] == "refresh-token"


def test_secret_service_errors_have_no_plaintext_fallback():
    class BrokenSecret(_Secret):
        def password_store_sync(self, *args):
            raise RuntimeError("locked")

    try:
        CalendarSecrets(BrokenSecret()).save("refresh-token")
    except SecretServiceError:
        pass
    else:
        raise AssertionError("Secret Service failure must not store a fallback")
