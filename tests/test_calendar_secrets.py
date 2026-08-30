"""Tests for injected Secret Service storage without a desktop D-Bus."""

import json

from meeting_recorder.calendar_secrets import (
    CalendarSecrets,
    SecretServiceError,
    validate_client_secret,
)


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
        return True

    def password_clear_sync(self, schema, attributes, cancellable):
        self.calls.append(("clear", schema, attributes, cancellable))
        self.token = None
        return True


class _BoundSecret(_Secret):
    """Keep the refresh item and fixed-account client-secret item separate."""

    def __init__(self, *, false_client_clear=False):
        super().__init__()
        self.values = {}
        self.false_client_clear = false_client_clear

    def password_lookup_sync(self, schema, attributes, cancellable):
        self.calls.append(("load", schema, attributes, cancellable))
        return self.values.get(tuple(sorted(attributes.items())))

    def password_store_sync(self, schema, attributes, collection, label, token, cancellable):
        self.calls.append(("save", schema, attributes, collection, label, token, cancellable))
        self.values[tuple(sorted(attributes.items()))] = token
        return True

    def password_clear_sync(self, schema, attributes, cancellable):
        self.calls.append(("clear", schema, attributes, cancellable))
        if attributes.get("account") == "google-calendar-client-secret" and self.false_client_clear:
            return False
        self.values.pop(tuple(sorted(attributes.items())), None)
        return True


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


def test_secret_service_rejects_false_store_and_clear_results():
    class FalseResultSecret(_Secret):
        def password_store_sync(self, schema, attributes, collection, label, token, cancellable):
            return False

        def password_clear_sync(self, schema, attributes, cancellable):
            return False

    store = CalendarSecrets(FalseResultSecret())
    try:
        store.save("refresh-token")
    except SecretServiceError:
        pass
    else:
        raise AssertionError("false secure-storage result was accepted")
    try:
        store.clear()
    except SecretServiceError:
        pass
    else:
        raise AssertionError("false secret-clear result was accepted")


def test_client_secret_is_a_separate_item_bound_to_the_public_client_id():
    api = _BoundSecret()
    store = CalendarSecrets(api)
    client_id = "12345-example.apps.googleusercontent.com"
    store.save("refresh-token")
    store.save_client_secret(client_id, "desktop-secret")

    assert store.load() == "refresh-token"
    assert store.client_secret_status(client_id) == "configured"
    assert store.client_secret_status("other.apps.googleusercontent.com") == "client-ID mismatch"
    assert store.load_client_secret(client_id) == "desktop-secret"
    client_calls = [call for call in api.calls if len(call) > 2 and
                    call[2].get("account") == "google-calendar-client-secret"]
    assert client_calls and all(call[2] == {"account": "google-calendar-client-secret"}
                                for call in client_calls)
    payload = api.values[(('account', 'google-calendar-client-secret'),)]
    assert json.loads(payload) == {
        "schema_version": 1,
        "client_id": client_id,
        "client_secret": "desktop-secret",
    }

    store.clear()
    assert store.load_client_secret(client_id) == "desktop-secret"
    store.clear_client_secret()
    assert store.client_secret_status(client_id) == "absent"
    assert store.load_client_secret(client_id) is None


def test_client_secret_payload_is_strict_and_mismatch_is_safe():
    api = _BoundSecret()
    store = CalendarSecrets(api)
    client_id = "12345-example.apps.googleusercontent.com"
    key = (('account', 'google-calendar-client-secret'),)

    for payload in (
        '{"schema_version":1,"client_id":"%s","client_secret":"secret","extra":1}' % client_id,
        '{"schema_version":true,"client_id":"%s","client_secret":"secret"}' % client_id,
        '{"schema_version":1,"client_id":"%s","client_secret":"secret",'
        '"client_secret":"other"}' % client_id,
    ):
        api.values[key] = payload
        try:
            store.client_secret_status(client_id)
        except SecretServiceError as exc:
            assert "malformed" in str(exc) and "desktop-secret" not in str(exc)
        else:
            raise AssertionError("malformed status payload was accepted")
        try:
            store.load_client_secret(client_id)
        except SecretServiceError as exc:
            assert "malformed" in str(exc) and "desktop-secret" not in str(exc)
        else:
            raise AssertionError("malformed client-secret payload was accepted")

    api.values[key] = json.dumps({
        "schema_version": 1, "client_id": "other.apps.googleusercontent.com",
        "client_secret": "desktop-secret",
    })
    assert store.client_secret_status(client_id) == "client-ID mismatch"
    try:
        store.load_client_secret(client_id)
    except SecretServiceError as exc:
        assert str(exc) == "Stored Google Calendar client secret does not match"
    else:
        raise AssertionError("mismatched client-secret payload was accepted")


def test_client_secret_replacement_uses_one_item_and_clear_is_idempotent():
    api = _BoundSecret(false_client_clear=True)
    store = CalendarSecrets(api)
    first = "12345-first.apps.googleusercontent.com"
    second = "12345-second.apps.googleusercontent.com"
    store.save_client_secret(first, "first-secret")
    store.save_client_secret(second, "second-secret")

    try:
        store.load_client_secret(first)
    except SecretServiceError as exc:
        assert str(exc) == "Stored Google Calendar client secret does not match"
    else:
        raise AssertionError("replaced client-secret binding was accepted")
    assert store.load_client_secret(second) == "second-secret"
    assert list(api.values) == [(('account', 'google-calendar-client-secret'),)]
    api.values.clear()
    store.clear_client_secret()
    assert store.load_client_secret(second) is None


def test_client_secret_input_rejects_empty_oversized_and_ascii_control_values():
    for value in ("", "x" * 4097, "before\nafter", "before\x7fafter", None, 7):
        try:
            validate_client_secret(value)
        except SecretServiceError as exc:
            assert "before" not in str(exc) and "after" not in str(exc)
        else:
            raise AssertionError("unsafe client-secret input was accepted")
