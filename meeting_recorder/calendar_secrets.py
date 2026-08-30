"""Secret Service storage for Google Calendar OAuth credentials.

This module deliberately imports PyGObject only when Calendar is used.  The
recording service therefore remains usable on systems without Secret Service.
"""

from __future__ import annotations

import json
from typing import Any


_SCHEMA_NAME = "org.meeting_recorder.GoogleCalendar"
_ACCOUNT_KEY = "account"
_ACCOUNT_VALUE = "google-calendar-refresh-token"
_LABEL = "Meeting Recorder Google Calendar refresh token"
_CLIENT_SECRET_SCHEMA_NAME = "org.meeting_recorder.GoogleCalendarClientSecret"
_CLIENT_SECRET_ACCOUNT = "google-calendar-client-secret"
_CLIENT_SECRET_LABEL = "Meeting Recorder Google Calendar client secret"
_CLIENT_SECRET_SCHEMA_VERSION = 1
_CLIENT_SECRET_MAX_LENGTH = 4096
_CLIENT_SECRET_PAYLOAD_MAX_BYTES = 16384
_CLIENT_SECRET_KEYS = frozenset({"schema_version", "client_id", "client_secret"})


class SecretServiceError(RuntimeError):
    """Secret Service is unavailable, locked, or rejected an operation."""


def _load_secret_api() -> Any:
    """Import Secret lazily so Calendar remains an optional desktop feature."""
    try:
        import gi

        gi.require_version("Secret", "1")
        from gi.repository import Secret
        return Secret
    except Exception as exc:
        raise SecretServiceError("Secret Service is unavailable or locked") from exc


def validate_client_secret(value: Any) -> str:
    """Validate one hidden client-secret input without retaining or echoing it."""
    if not isinstance(value, str):
        raise SecretServiceError("Google Calendar client secret is invalid")
    if not value or len(value) > _CLIENT_SECRET_MAX_LENGTH:
        raise SecretServiceError("Google Calendar client secret is invalid")
    # Reject ASCII controls, including DEL, because this input must stay one line.
    if any(value_char.isascii() and (ord(value_char) < 0x20 or ord(value_char) == 0x7F)
           for value_char in value):
        raise SecretServiceError("Google Calendar client secret is invalid")
    return value


class CalendarSecrets:
    """Store refresh and client-secret credentials as separate Secret items."""

    def __init__(self, secret_api: Any | None = None) -> None:
        self._secret_api = secret_api
        self._schema: Any | None = None
        self._client_secret_schema: Any | None = None

    def _api(self) -> Any:
        return self._secret_api if self._secret_api is not None else _load_secret_api()

    def _get_schema(self) -> Any:
        if self._schema is None:
            api = self._api()
            try:
                self._schema = api.Schema.new(
                    _SCHEMA_NAME,
                    api.SchemaFlags.NONE,
                    {_ACCOUNT_KEY: api.SchemaAttributeType.STRING},
                )
            except Exception as exc:
                raise SecretServiceError("Secret Service schema is unavailable") from exc
        return self._schema

    def _get_client_secret_schema(self) -> Any:
        """Build a separate fixed-account schema for the one payload item."""
        if self._client_secret_schema is None:
            api = self._api()
            try:
                self._client_secret_schema = api.Schema.new(
                    _CLIENT_SECRET_SCHEMA_NAME,
                    api.SchemaFlags.NONE,
                    {_ACCOUNT_KEY: api.SchemaAttributeType.STRING},
                )
            except Exception as exc:
                raise SecretServiceError("Secret Service schema is unavailable") from exc
        return self._client_secret_schema

    @staticmethod
    def _attributes() -> dict[str, str]:
        return {_ACCOUNT_KEY: _ACCOUNT_VALUE}

    @staticmethod
    def _client_secret_attributes() -> dict[str, str]:
        return {_ACCOUNT_KEY: _CLIENT_SECRET_ACCOUNT}

    @staticmethod
    def _validate_client_binding(client_id: Any) -> str:
        """Validate the payload's public-client binding without importing OAuth."""
        if (not isinstance(client_id, str) or not client_id or len(client_id) > 1024 or
                any(char.isascii() and (ord(char) < 0x20 or ord(char) == 0x7F)
                    for char in client_id)):
            raise SecretServiceError("Google Calendar client ID is malformed")
        return client_id

    def load(self) -> str | None:
        """Return the stored refresh token, never falling back to plaintext."""
        try:
            token = self._api().password_lookup_sync(
                self._get_schema(), self._attributes(), None)
        except SecretServiceError:
            raise
        except Exception as exc:
            raise SecretServiceError("Secret Service is unavailable or locked") from exc
        if token is None:
            return None
        if not isinstance(token, str) or not token:
            raise SecretServiceError("Secret Service returned an invalid credential")
        return token

    def save(self, refresh_token: str) -> None:
        """Store a refresh token and no other OAuth material."""
        if not isinstance(refresh_token, str) or not refresh_token:
            raise SecretServiceError("Refusing an empty refresh token")
        try:
            stored = self._api().password_store_sync(
                self._get_schema(), self._attributes(),
                self._api().COLLECTION_DEFAULT, _LABEL, refresh_token, None)
        except SecretServiceError:
            raise
        except Exception as exc:
            raise SecretServiceError("Secret Service could not store the credential") from exc
        if stored is False:
            raise SecretServiceError("Secret Service could not store the credential")

    def clear(self) -> None:
        """Delete this application's refresh token without affecting other secrets."""
        try:
            cleared = self._api().password_clear_sync(
                self._get_schema(), self._attributes(), None)
        except SecretServiceError:
            raise
        except Exception as exc:
            raise SecretServiceError("Secret Service could not clear the credential") from exc
        if cleared is False:
            raise SecretServiceError("Secret Service could not clear the credential")

    @staticmethod
    def _strict_object(pairs: list[tuple[Any, Any]]) -> dict[str, Any]:
        """Reject duplicate JSON keys before the payload can be interpreted."""
        result: dict[str, Any] = {}
        # Reject duplicate names instead of letting JSON decoding overwrite one.
        for key, value in pairs:
            if key in result:
                raise ValueError
            result[key] = value
        return result

    @classmethod
    def _encode_client_secret(cls, client_id: str, client_secret: str) -> str:
        """Encode the bounded versioned payload used by the single Secret item."""
        binding = cls._validate_client_binding(client_id)
        secret = validate_client_secret(client_secret)
        # Use a compact ASCII form so payload size and key order are predictable.
        payload = json.dumps(
            {
                "schema_version": _CLIENT_SECRET_SCHEMA_VERSION,
                "client_id": binding,
                "client_secret": secret,
            },
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        if len(payload.encode("ascii")) > _CLIENT_SECRET_PAYLOAD_MAX_BYTES:
            raise SecretServiceError("Google Calendar client secret is invalid")
        return payload

    @classmethod
    def _decode_client_secret(cls, payload: Any) -> tuple[str, str]:
        """Parse one exact payload shape without exposing any stored value."""
        if not isinstance(payload, str) or not payload:
            raise SecretServiceError("Stored Google Calendar client secret is malformed")
        try:
            payload_size = len(payload.encode("utf-8"))
        except UnicodeError:
            raise SecretServiceError("Stored Google Calendar client secret is malformed") from None
        if payload_size > _CLIENT_SECRET_PAYLOAD_MAX_BYTES:
            raise SecretServiceError("Stored Google Calendar client secret is malformed")
        # Decode with duplicate-key rejection before checking the exact schema.
        try:
            decoded = json.loads(payload, object_pairs_hook=cls._strict_object)
        except (TypeError, ValueError, UnicodeError):
            raise SecretServiceError("Stored Google Calendar client secret is malformed") from None
        if (not isinstance(decoded, dict) or set(decoded) != _CLIENT_SECRET_KEYS or
                type(decoded["schema_version"]) is not int or
                decoded["schema_version"] != _CLIENT_SECRET_SCHEMA_VERSION):
            raise SecretServiceError("Stored Google Calendar client secret is malformed")
        try:
            client_id = cls._validate_client_binding(decoded["client_id"])
            client_secret = validate_client_secret(decoded["client_secret"])
        except SecretServiceError:
            raise SecretServiceError("Stored Google Calendar client secret is malformed") from None
        return client_id, client_secret

    def _load_client_secret_payload(self) -> str | None:
        """Load the one fixed-account item without searching for stale bindings."""
        try:
            payload = self._api().password_lookup_sync(
                self._get_client_secret_schema(), self._client_secret_attributes(), None)
        except SecretServiceError:
            raise SecretServiceError("Secret Service is unavailable or locked") from None
        except Exception:
            raise SecretServiceError("Secret Service is unavailable or locked") from None
        return payload

    def client_secret_status(self, client_id: str) -> str:
        """Return only absent, configured, or client-ID mismatch semantics."""
        binding = self._validate_client_binding(client_id)
        payload = self._load_client_secret_payload()
        if payload is None:
            return "absent"
        # A valid payload can report only a matching or mismatched public client ID.
        stored_id, _secret = self._decode_client_secret(payload)
        return "configured" if stored_id == binding else "client-ID mismatch"

    def load_client_secret(self, client_id: str) -> str | None:
        """Return the secret only when the strict payload binds to this client ID."""
        binding = self._validate_client_binding(client_id)
        payload = self._load_client_secret_payload()
        if payload is None:
            return None
        stored_id, client_secret = self._decode_client_secret(payload)
        if stored_id != binding:
            raise SecretServiceError("Stored Google Calendar client secret does not match")
        return client_secret

    def save_client_secret(self, client_id: str, client_secret: str) -> None:
        """Replace the one fixed-account item with a strict bound payload."""
        payload = self._encode_client_secret(client_id, client_secret)
        try:
            stored = self._api().password_store_sync(
                self._get_client_secret_schema(), self._client_secret_attributes(),
                self._api().COLLECTION_DEFAULT, _CLIENT_SECRET_LABEL, payload, None)
        except SecretServiceError:
            raise SecretServiceError("Secret Service could not store the credential") from None
        except Exception:
            raise SecretServiceError("Secret Service could not store the credential") from None
        if stored is False:
            raise SecretServiceError("Secret Service could not store the credential")

    def clear_client_secret(self) -> None:
        """Clear the one client-secret item; absence is already a successful clear."""
        try:
            cleared = self._api().password_clear_sync(
                self._get_client_secret_schema(), self._client_secret_attributes(), None)
        except SecretServiceError:
            raise SecretServiceError("Secret Service could not clear the credential") from None
        except Exception:
            raise SecretServiceError("Secret Service could not clear the credential") from None
        # Secret Service reports False when no matching item exists; make clear idempotent.
        if cleared is False:
            return
