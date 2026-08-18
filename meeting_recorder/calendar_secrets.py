"""Secret Service storage for the Google Calendar refresh token.

This module deliberately imports PyGObject only when Calendar is used.  The
recording service therefore remains usable on systems without Secret Service.
"""

from __future__ import annotations

from typing import Any


_SCHEMA_NAME = "org.meeting_recorder.GoogleCalendar"
_ACCOUNT_KEY = "account"
_ACCOUNT_VALUE = "google-calendar-refresh-token"
_LABEL = "Meeting Recorder Google Calendar refresh token"


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


class CalendarSecrets:
    """Store only the raw refresh token under this application's one account."""

    def __init__(self, secret_api: Any | None = None) -> None:
        self._secret_api = secret_api
        self._schema: Any | None = None

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

    @staticmethod
    def _attributes() -> dict[str, str]:
        return {_ACCOUNT_KEY: _ACCOUNT_VALUE}

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
        """Delete this application's token without affecting other secrets."""
        try:
            cleared = self._api().password_clear_sync(
                self._get_schema(), self._attributes(), None)
        except SecretServiceError:
            raise
        except Exception as exc:
            raise SecretServiceError("Secret Service could not clear the credential") from exc
        if cleared is False:
            raise SecretServiceError("Secret Service could not clear the credential")
