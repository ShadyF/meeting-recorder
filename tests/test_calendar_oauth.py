"""Deterministic OAuth credential tests using injected browser, server, and HTTP."""

import os
import tempfile
import urllib.parse
from types import SimpleNamespace

from meeting_recorder.calendar_oauth import (
    CalendarOAuth,
    CalendarStatus,
    SCOPES,
    build_authorization_url,
    create_pkce_verifier,
    parse_callback,
    pkce_challenge,
    validate_client_id,
    validate_loopback_port,
)
from meeting_recorder.calendar_secrets import SecretServiceError


CLIENT = "12345-example.apps.googleusercontent.com"


class _Secrets:
    def __init__(self, token=None, fail_save=False, fail_clear=False):
        self.token = token
        self.fail_save = fail_save
        self.fail_clear = fail_clear
        self.cleared = False

    def load(self):
        return self.token

    def save(self, token):
        if self.fail_save:
            raise SecretServiceError("locked")
        self.token = token

    def clear(self):
        self.cleared = True
        if self.fail_clear:
            raise SecretServiceError("locked")
        self.token = None


class _Server:
    def __init__(self, port=43123):
        self.server_address = ("127.0.0.1", port)
        self.closed = False

    def server_close(self):
        self.closed = True


def _config(client=CLIENT, port=0):
    return SimpleNamespace(google_calendar_client_id=client,
                           google_calendar_loopback_port=port)


def _response(**extra):
    body = {"refresh_token": "new-refresh", "access_token": "short-lived",
            "scope": " ".join(SCOPES)}
    body.update(extra)
    import json
    return 200, json.dumps(body).encode()


def test_pkce_authorization_parameters_and_exact_scopes():
    verifier = create_pkce_verifier()
    assert 43 <= len(verifier) <= 128
    assert pkce_challenge("abc") == "ungWv48Bz-pBQUDeXa4iI7ADYaOWF3qctBD_YfIAFa0"
    url = build_authorization_url(CLIENT, "http://127.0.0.1:1/oauth2/callback",
                                  "state", verifier)
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
    assert query["scope"] == [" ".join(SCOPES)]
    assert query["access_type"] == ["offline"]
    assert query["include_granted_scopes"] == ["true"]
    assert query["prompt"] == ["consent"]
    assert query["code_challenge_method"] == ["S256"]


def test_callback_rejects_wrong_path_errors_duplicate_and_bad_state():
    rejected = (
        "/wrong?code=x&state=expected",
        "/oauth2/callback?error=access_denied&state=expected",
        "/oauth2/callback?code=x&code=y&state=expected",
        "/oauth2/callback?code=x&state=other",
    )
    for callback in rejected:
        try:
            parse_callback(callback, "expected")
        except Exception:
            pass
        else:
            raise AssertionError("unsafe callback was accepted")
    assert parse_callback("/oauth2/callback?code=x&state=expected", "expected") == "x"


def test_connect_binds_before_browser_and_sends_no_client_secret():
    calls = []
    server = _Server(49152)

    def factory(address, handler):
        calls.append(("bind", address, handler))
        return server

    def browser(url):
        calls.append(("browser", url))
        return True

    def post(url, data, timeout):
        calls.append(("post", url, data, timeout))
        return _response()

    oauth = CalendarOAuth(_config(), secret_store=_Secrets(), post_form=post,
                         browser_open=browser, server_factory=factory)
    oauth._wait_for_callback = lambda _server, _state: "authorization-code"
    oauth.connect()
    assert calls[0][0] == "bind"
    assert calls[1][0] == "browser"
    request = calls[2][2]
    assert request["redirect_uri"] == "http://127.0.0.1:49152/oauth2/callback"
    assert request["grant_type"] == "authorization_code"
    assert "client_secret" not in request
    assert server.closed


def test_fixed_port_failure_happens_before_browser_launch():
    opened = []

    def unavailable(*_args):
        raise OSError("in use")

    oauth = CalendarOAuth(_config(port=48291), secret_store=_Secrets(),
                         browser_open=lambda _url: opened.append(True) or True,
                         server_factory=unavailable)
    try:
        oauth.connect()
    except Exception:
        pass
    else:
        raise AssertionError("fixed port bind failure was accepted")
    assert not opened


def test_connect_requires_refresh_token_and_scopes_and_revokes_on_storage_failure():
    revoked = []

    def post(url, data, timeout):
        if "revoke" in url:
            revoked.append(data)
            return 200, b""
        return _response()

    oauth = CalendarOAuth(_config(), secret_store=_Secrets(fail_save=True), post_form=post,
                         browser_open=lambda _url: True, server_factory=lambda *_: _Server())
    oauth._wait_for_callback = lambda _server, _state: "authorization-code"
    try:
        oauth.connect()
    except Exception:
        pass
    else:
        raise AssertionError("unsecured token storage was accepted")
    assert revoked == [{"token": "new-refresh"}]

    for body in ({"refresh_token": "", "scope": " ".join(SCOPES)},
                 {"refresh_token": "new-refresh", "scope": SCOPES[0]}):
        attempt = CalendarOAuth(_config(), secret_store=_Secrets(),
                                post_form=lambda *_args, body=body: _response(**body),
                                browser_open=lambda _url: True,
                                server_factory=lambda *_: _Server())
        attempt._wait_for_callback = lambda _server, _state: "authorization-code"
        try:
            attempt.connect()
        except Exception:
            pass
        else:
            raise AssertionError("incomplete OAuth grant was accepted")


def test_status_maps_expiry_transient_and_scope_deficiency_without_clearing_token():
    token = _Secrets("saved-refresh")

    def invalid_grant(*_args):
        return 400, b'{"error":"invalid_grant"}'

    assert CalendarOAuth(_config(), secret_store=token, post_form=invalid_grant).status().state == "expired"

    def unavailable(*_args):
        return 503, b"not json"

    transient = CalendarOAuth(_config(), secret_store=token, post_form=unavailable).status()
    assert transient == CalendarStatus("misconfigured", "credential present but validation unavailable", 1)
    assert token.token == "saved-refresh"
    deficient = CalendarOAuth(_config(), secret_store=token,
                              post_form=lambda *_: _response(scope=SCOPES[0])).status()
    assert deficient.state == "misconfigured"


def test_status_disconnected_and_validation_rules_make_no_google_call():
    calls = []
    status = CalendarOAuth(_config(client=""), secret_store=_Secrets("token"),
                           post_form=lambda *_: calls.append(True)).status()
    assert status.state == "disconnected" and not calls
    assert CalendarOAuth(_config(port=True), secret_store=_Secrets(),
                         post_form=lambda *_: calls.append(True)).status().state == "misconfigured"
    for invalid in (" client.apps.googleusercontent.com", "https://x.apps.googleusercontent.com",
                    "{\"client_id\":\"x\"}", "x/apps.googleusercontent.com"):
        try:
            validate_client_id(invalid)
        except Exception:
            pass
        else:
            raise AssertionError("unsafe client ID was accepted")
    for invalid in (True, 1.5, -1, 65536):
        try:
            validate_loopback_port(invalid)
        except Exception:
            pass
        else:
            raise AssertionError("unsafe port was accepted")


def test_disconnect_clears_local_token_and_calendar_cache_when_revoke_fails():
    previous = os.environ.get("XDG_CACHE_HOME")
    with tempfile.TemporaryDirectory() as temp:
        os.environ["XDG_CACHE_HOME"] = temp
        cache = os.path.join(temp, "meeting-recorder", "google-calendar")
        os.makedirs(cache)
        with open(os.path.join(cache, "state"), "w", encoding="utf-8") as fh:
            fh.write("private")
        store = _Secrets("saved-refresh")

        def revoke_fails(*_args):
            raise OSError("offline")

        result = CalendarOAuth(_config(client="", port=0), secret_store=store,
                               post_form=revoke_fails).disconnect()
        assert result.state == "disconnected"
        assert store.cleared and store.token is None and not os.path.exists(cache)
    if previous is None:
        os.environ.pop("XDG_CACHE_HOME", None)
    else:
        os.environ["XDG_CACHE_HOME"] = previous
