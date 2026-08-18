"""Bounded, headless Google Calendar OAuth credential management only."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Callable

from .calendar_secrets import CalendarSecrets, SecretServiceError
from .calendar_cache import CalendarCache, calendar_operation_lock


AUTHORIZATION_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOCATION_URL = "https://oauth2.googleapis.com/revoke"
SCOPES = (
    "https://www.googleapis.com/auth/calendar.calendarlist.readonly",
    "https://www.googleapis.com/auth/calendar.events.readonly",
)
_CLIENT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*\.apps\.googleusercontent\.com$")
_CALLBACK_PATH = "/oauth2/callback"
_CONNECTION_TIMEOUT_SECONDS = 10


class CalendarError(RuntimeError):
    """Base class for Calendar credential management failures."""


class CalendarConfigurationError(CalendarError):
    """The local Calendar configuration or credential is unsafe or malformed."""


class CalendarAuthorizationDeniedError(CalendarError):
    """The user explicitly denied or cancelled an otherwise valid OAuth callback."""


class CalendarUnavailableError(CalendarError):
    """A transient network problem prevented credential validation."""


class CalendarExpiredError(CalendarError):
    """Google definitively rejected a stored refresh token."""


@dataclass(frozen=True)
class CalendarStatus:
    state: str
    detail: str = ""
    exit_code: int = 0


def validate_client_id(client_id: Any) -> str:
    """Accept only a bare Desktop OAuth client ID, never a secret or JSON blob."""
    if not isinstance(client_id, str) or not _CLIENT_ID.fullmatch(client_id):
        raise CalendarConfigurationError("Google Calendar client ID is malformed")
    return client_id


def validate_loopback_port(port: Any) -> int:
    """Validate the explicit loopback listener port without coercing JSON types."""
    if isinstance(port, bool) or not isinstance(port, int) or not 0 <= port <= 65535:
        raise CalendarConfigurationError("Google Calendar loopback port must be 0 or 1..65535")
    return port


def create_pkce_verifier() -> str:
    """Create an RFC 7636 verifier in the required 43--128 character range."""
    return secrets.token_urlsafe(64)[:128]


def pkce_challenge(verifier: str) -> str:
    """Return the unpadded S256 challenge for an OAuth verifier."""
    return base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")


def build_authorization_url(client_id: str, redirect_uri: str, state: str,
                            verifier: str) -> str:
    """Build the installed-app authorization request with the exact least scopes."""
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(SCOPES),
        "access_type": "offline",
        "include_granted_scopes": "true",
        "prompt": "consent",
        "code_challenge": pkce_challenge(verifier),
        "code_challenge_method": "S256",
        "state": state,
    }
    return f"{AUTHORIZATION_URL}?{urllib.parse.urlencode(params)}"


def parse_callback(target: str, expected_state: str) -> str:
    """Validate a single callback without exposing its query values in errors."""
    parsed = urllib.parse.urlsplit(target)
    values = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
    if parsed.path != _CALLBACK_PATH:
        raise CalendarConfigurationError("OAuth callback used an unexpected path")
    states = values.get("state", [])
    if len(states) != 1 or not states[0]:
        raise CalendarConfigurationError("OAuth callback must contain one state")
    if not hmac.compare_digest(states[0], expected_state):
        raise CalendarConfigurationError("OAuth callback state did not match")
    errors = values.get("error", [])
    codes = values.get("code", [])
    if errors:
        if len(errors) == 1 and errors[0] and not codes:
            raise CalendarAuthorizationDeniedError("Google authorization was denied or cancelled")
        raise CalendarConfigurationError("OAuth callback error is malformed")
    if len(codes) != 1 or not codes[0]:
        raise CalendarConfigurationError("OAuth callback must contain one code")
    return codes[0]


def calendar_cache_path() -> Path:
    """Return the dedicated Calendar cache subtree, never the recording output."""
    base = os.environ.get("XDG_CACHE_HOME") or "~/.cache"
    return Path(os.path.expanduser(base)) / "meeting-recorder" / "google-calendar"


def clear_calendar_cache() -> None:
    """Remove only Calendar's private cache subtree."""
    try:
        shutil.rmtree(calendar_cache_path())
    except FileNotFoundError:
        # A missing dedicated subtree already confirms local cache removal.
        return


def _post_form(url: str, data: dict[str, str], timeout: float) -> tuple[int, bytes]:
    """POST form data with the standard library and no OAuth logging."""
    request = urllib.request.Request(
        url, data=urllib.parse.urlencode(data).encode("ascii"), method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()
    except (urllib.error.URLError, socket.timeout, TimeoutError, OSError) as exc:
        raise CalendarUnavailableError("Google validation is temporarily unavailable") from exc


def _json_response(body: bytes) -> dict[str, Any]:
    try:
        data = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CalendarConfigurationError("Google returned a malformed credential response") from exc
    if not isinstance(data, dict):
        raise CalendarConfigurationError("Google returned a malformed credential response")
    return data


def _required_scopes(data: dict[str, Any]) -> bool:
    scopes = data.get("scope")
    return isinstance(scopes, str) and set(SCOPES).issubset(scopes.split())


def _retryable_403(response: dict[str, Any]) -> bool:
    """Recognize Google rate-limit payloads without treating all 403s as transient."""
    error = response.get("error")
    values: list[str] = []
    if isinstance(error, str):
        values.append(error)
    elif isinstance(error, dict):
        values.extend(str(error.get(key, "")) for key in ("status", "message"))
        details = error.get("errors", [])
        if isinstance(details, list):
            values.extend(str(item.get("reason", "")) for item in details
                          if isinstance(item, dict))
    normalized = " ".join(values).lower().replace("_", "").replace(" ", "").replace("-", "")
    return any(term in normalized for term in
               ("ratelimit", "quota", "retry", "temporarily", "resourceexhausted"))


class CalendarOAuth:
    """OAuth workflow with injectable I/O for deterministic, network-free tests."""

    def __init__(self, config: Any, *, secret_store: CalendarSecrets | None = None,
                 post_form: Callable[[str, dict[str, str], float], tuple[int, bytes]] = _post_form,
                 browser_open: Callable[[str], bool] = webbrowser.open,
                 server_factory: Callable[..., HTTPServer] = HTTPServer,
                 cache_clear: Callable[[], None] = clear_calendar_cache,
                 callback_timeout: float = 120, max_callback_requests: int = 3) -> None:
        self.config = config
        self.secrets = secret_store or CalendarSecrets()
        self._post_form = post_form
        self._browser_open = browser_open
        self._server_factory = server_factory
        self._cache_clear = cache_clear
        self._callback_timeout = callback_timeout
        self._max_callback_requests = max_callback_requests

    def _configuration(self) -> tuple[str | None, int]:
        port = validate_loopback_port(self.config.google_calendar_loopback_port)
        raw_client = self.config.google_calendar_client_id
        if raw_client == "":
            return None, port
        return validate_client_id(raw_client), port

    def _request_token(self, data: dict[str, str]) -> dict[str, Any]:
        status, body = self._post_form(TOKEN_URL, data, 15)
        if status >= 500:
            raise CalendarUnavailableError("Google validation is temporarily unavailable")
        if status in (408, 429):
            raise CalendarUnavailableError("Google validation is temporarily unavailable")
        response = _json_response(body)
        if status == 403 and _retryable_403(response):
            raise CalendarUnavailableError("Google validation is temporarily unavailable")
        if status >= 400:
            error = response.get("error")
            if error == "invalid_grant":
                raise CalendarExpiredError("Google rejected the saved refresh token")
            if error == "invalid_client":
                raise CalendarConfigurationError("Google rejected the configured client")
            raise CalendarConfigurationError("Google rejected the credential request")
        return response

    def _revoke(self, token: str) -> None:
        """Try revocation without allowing cleanup to depend on the result."""
        try:
            self._post_form(REVOCATION_URL, {"token": token}, 10)
        except Exception:
            pass

    def _wait_for_callback(self, server: HTTPServer, expected_state: str) -> str:
        result: dict[str, str] = {}

        class CallbackHandler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 - required by BaseHTTPRequestHandler
                try:
                    result["code"] = parse_callback(self.path, expected_state)
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(b"Authorization received. You may close this window.")
                except CalendarAuthorizationDeniedError:
                    result["denied"] = "true"
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(b"Authorization was cancelled.")
                except CalendarConfigurationError:
                    self.send_response(400)
                    self.end_headers()
                    self.wfile.write(b"Authorization callback rejected.")

            def log_message(self, format: str, *_args: Any) -> None:
                # BaseHTTPRequestHandler would log callback query strings.
                return

        # Restore inherited attributes by deleting temporary instance overrides.
        original_handler = getattr(server, "RequestHandlerClass", None)
        original_timeout = getattr(server, "timeout", None)
        instance_attributes = vars(server)
        had_handler = "RequestHandlerClass" in instance_attributes
        had_timeout = "timeout" in instance_attributes
        had_get_request = "get_request" in instance_attributes
        original_get_request = getattr(server, "get_request", None)
        deadline = time.monotonic() + self._callback_timeout
        try:
            # Replace the temporary handler assigned at bind time before accepting.
            server.RequestHandlerClass = CallbackHandler

            # Bound each accepted socket so a partial local request cannot hold the
            # single-threaded listener past the overall authorization deadline.
            if original_get_request is not None:
                def get_request_with_timeout():
                    request, address = original_get_request()
                    remaining = max(0.001, deadline - time.monotonic())
                    request.settimeout(min(_CONNECTION_TIMEOUT_SECONDS, remaining))
                    return request, address

                server.get_request = get_request_with_timeout
            for _ in range(self._max_callback_requests):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                server.timeout = remaining
                server.handle_request()
                if "denied" in result:
                    raise CalendarAuthorizationDeniedError(
                        "Google authorization was denied or cancelled")
                if "code" in result:
                    return result["code"]
            raise CalendarUnavailableError("OAuth callback timed out")
        finally:
            # Do not retain closures over the bound server method after the listener closes.
            if had_handler:
                server.RequestHandlerClass = original_handler
            else:
                delattr(server, "RequestHandlerClass")
            if had_timeout:
                server.timeout = original_timeout
            else:
                delattr(server, "timeout")
            if had_get_request:
                server.get_request = original_get_request
            elif original_get_request is not None:
                delattr(server, "get_request")

    def connect(self) -> None:
        """Complete authorization and store only a validated refresh token."""
        client_id, port = self._configuration()
        if client_id is None:
            raise CalendarConfigurationError("Google Calendar client ID is not configured")

        # Binding precedes browser launch so consent never targets a dead listener.
        try:
            server = self._server_factory(("127.0.0.1", port), BaseHTTPRequestHandler)
        except OSError as exc:
            raise CalendarUnavailableError(
                "Could not bind the Google OAuth loopback listener") from exc
        try:
            actual_port = server.server_address[1]
            redirect_uri = f"http://127.0.0.1:{actual_port}{_CALLBACK_PATH}"
            verifier = create_pkce_verifier()
            state = secrets.token_urlsafe(32)
            url = build_authorization_url(client_id, redirect_uri, state, verifier)
            if not self._browser_open(url):
                raise CalendarUnavailableError("Could not open a browser for Google authorization")
            code = self._wait_for_callback(server, state)
        finally:
            server.server_close()

        response = self._request_token({
            "code": code,
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
            "code_verifier": verifier,
        })
        refresh_token = response.get("refresh_token")
        if not isinstance(refresh_token, str) or not refresh_token or not _required_scopes(response):
            raise CalendarConfigurationError("Google did not grant a usable Calendar refresh token")
        try:
            with calendar_operation_lock(blocking=True, root=CalendarCache().root) as acquired:
                if not acquired:
                    raise SecretServiceError("Calendar operation lock is unavailable")
                # Remove snapshots issued for a prior credential before committing the replacement.
                self._cache_clear()
                self.secrets.save(refresh_token)
        except (SecretServiceError, OSError):
            self._revoke(refresh_token)
            raise CalendarConfigurationError("Secret Service could not securely store the credential")

    def status(self) -> CalendarStatus:
        """Validate a stored credential without retaining response access tokens."""
        try:
            client_id, _port = self._configuration()
        except CalendarConfigurationError as exc:
            return CalendarStatus("misconfigured", str(exc), 1)
        try:
            refresh_token = self.secrets.load()
        except SecretServiceError as exc:
            return CalendarStatus("misconfigured", str(exc), 1)
        if client_id is None:
            if refresh_token:
                return CalendarStatus("misconfigured",
                                      "credential present but client ID is not configured", 1)
            return CalendarStatus("disconnected")
        if refresh_token is None:
            return CalendarStatus("disconnected")
        try:
            response = self._request_token({
                "client_id": client_id,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
            })
            if not isinstance(response.get("access_token"), str) or not response["access_token"]:
                raise CalendarConfigurationError("Google returned a malformed credential")
            if "scope" in response and not _required_scopes(response):
                raise CalendarConfigurationError("Google did not grant the required Calendar scopes")
        except CalendarExpiredError as exc:
            return CalendarStatus("expired", str(exc), 1)
        except CalendarUnavailableError:
            # Keep the token for diagnosis; a transient outage is not revocation.
            return CalendarStatus("connected", "credential present; validation unavailable", 1)
        except CalendarConfigurationError as exc:
            return CalendarStatus("misconfigured", str(exc), 1)
        return CalendarStatus("connected")

    def access_token(self) -> str:
        """Return one transient refresh-grant access token without persisting it."""
        client_id, _port = self._configuration()
        if client_id is None:
            raise CalendarConfigurationError("Google Calendar client ID is not configured")
        try:
            refresh_token = self.secrets.load()
        except SecretServiceError as exc:
            raise CalendarConfigurationError("Secret Service is unavailable or locked") from exc
        if refresh_token is None:
            raise CalendarConfigurationError("Google Calendar is disconnected")
        response = self._request_token({"client_id": client_id, "refresh_token": refresh_token,
                                        "grant_type": "refresh_token"})
        token = response.get("access_token")
        if not isinstance(token, str) or not token:
            raise CalendarConfigurationError("Google returned a malformed credential")
        if "scope" in response and not _required_scopes(response):
            raise CalendarConfigurationError("Google did not grant the required Calendar scopes")
        return token

    def disconnect(self) -> CalendarStatus:
        """Revoke best-effort and always remove local Calendar-only data."""
        token: str | None = None
        cleanup_errors: list[str] = []
        with calendar_operation_lock(blocking=True, root=CalendarCache().root) as acquired:
            if not acquired:
                return CalendarStatus("misconfigured", "Calendar operation lock is unavailable", 1)
            try:
                token = self.secrets.load()
            except SecretServiceError as exc:
                cleanup_errors.append(str(exc))
            try:
                if token:
                    self._revoke(token)
            finally:
                try:
                    self.secrets.clear()
                except SecretServiceError as exc:
                    cleanup_errors.append(str(exc))
                try:
                    self._cache_clear()
                except OSError:
                    cleanup_errors.append("Calendar cache could not be removed")
        if cleanup_errors:
            return CalendarStatus("misconfigured", "; ".join(cleanup_errors), 1)
        return CalendarStatus("disconnected")
