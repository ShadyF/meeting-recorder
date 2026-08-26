"""Deterministic OAuth credential tests using injected browser, server, and HTTP."""

import os
import tempfile
import threading
import urllib.error
import urllib.parse
from unittest.mock import patch
from types import SimpleNamespace

from meeting_recorder.calendar_oauth import (
    CalendarOAuth,
    CalendarAuthorizationDeniedError,
    CalendarConfigurationError,
    CalendarExpiredError,
    CalendarStatus,
    CalendarUnavailableError,
    SCOPES,
    _open_uri_with_portal,
    _portal_open_uri_response,
    _post_form,
    build_authorization_url,
    create_pkce_verifier,
    parse_callback,
    pkce_challenge,
    validate_client_id,
    validate_listener_address,
    validate_loopback_port,
)
from meeting_recorder.calendar_secrets import SecretServiceError
from meeting_recorder.calendar_cache import CalendarCache
from meeting_recorder.calendar_google import CalendarApiError, GoogleCalendarClient
from meeting_recorder.calendar_refresh import CalendarRefresher


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
    RequestHandlerClass = object
    timeout = None

    def __init__(self, port=43123):
        self.server_address = ("127.0.0.1", port)
        self.closed = False

    def server_close(self):
        self.closed = True


class _CallbackServer(_Server):
    """Drive the installed handler directly without a socket or browser."""

    def __init__(self, paths):
        super().__init__()
        self.paths = iter(paths)
        self.responses = []

    def handle_request(self):
        handler = object.__new__(self.RequestHandlerClass)
        handler.path = next(self.paths)
        handler.send_response = self.responses.append
        handler.end_headers = lambda: None
        handler.wfile = _Writer()
        handler.do_GET()


class _Writer:
    def write(self, _value):
        return None


class _PortalVariant:
    """Store the narrow Variant payloads used by the portal transport tests."""

    def __init__(self, value, signature=None):
        self.value = value
        self.signature = signature

    def unpack(self):
        return self.value


class _PortalHarness:
    """Provide just enough Gio and GLib behavior for portal transport tests."""

    def __init__(self, response=0, *, early=False, timeout=False):
        self.response = response
        self.early = early
        self.timeout = timeout
        self.request_path = "/org/freedesktop/portal/desktop/request/test"
        self.calls = []
        self.subscription = None
        self.unsubscribed = []
        self.removed_sources = []
        self.timeout_callback = None
        self.loop_ran = False
        self.loop = SimpleNamespace(quit=lambda: None, run=self._run)
        self.gio = SimpleNamespace(
            BusType=SimpleNamespace(SESSION="session"),
            DBusSignalFlags=SimpleNamespace(NONE="signal-none"),
            DBusCallFlags=SimpleNamespace(NONE="call-none"),
            bus_get_sync=lambda *_args: self,
        )
        self.glib = SimpleNamespace(
            MainLoop=lambda: self.loop,
            Variant=lambda signature, value: _PortalVariant(value, signature),
            timeout_add_seconds=self._add_timeout,
            source_remove=self.removed_sources.append,
        )

    def signal_subscribe(self, *args):
        # Record the exact subscription arguments and retain its callback for delivery.
        self.subscription = args
        return 9

    def signal_unsubscribe(self, subscription):
        # Record cleanup without needing a real session bus.
        self.unsubscribed.append(subscription)

    def call_sync(self, *args):
        # Return the request path and optionally deliver signals during the method call.
        self.calls.append(args)
        if args[3] == "OpenURI":
            if self.early:
                # Fill the bounded queue with unrelated signals before the matching response.
                for index in range(4):
                    self.subscription[-1](self, "sender", f"/other/{index}", "iface", "Response",
                                          _PortalVariant((0, {})), None)
                self.subscription[-1](self, "sender", self.request_path, "iface", "Response",
                                      _PortalVariant((self.response, {})), None)
            return _PortalVariant((self.request_path,))
        return _PortalVariant(())

    def _add_timeout(self, _seconds, callback):
        # Retain the bounded timeout callback until the fake loop runs.
        self.timeout_callback = callback
        return 7

    def _run(self):
        # Drive either the local timeout or a response after an unrelated request signal.
        self.loop_ran = True
        if self.timeout:
            self.timeout_callback()
            return
        self.subscription[-1](self, "sender", "/other/request", "iface", "Response",
                              _PortalVariant((0, {})), None)
        self.subscription[-1](self, "sender", self.request_path, "iface", "Response",
                              _PortalVariant((self.response, {})), None)


class _PartialRequest:
    def __init__(self):
        self.timeout = None

    def settimeout(self, timeout):
        self.timeout = timeout


class _PartialServer(_Server):
    """Simulate an accepted connection that never completes its request line."""

    def __init__(self):
        super().__init__()
        self.request = _PartialRequest()

    def get_request(self):
        return self.request, ("127.0.0.1", 1)

    def handle_request(self):
        self.get_request()


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
        "/oauth2/callback?error=access_denied",
        "/oauth2/callback?error=access_denied&error=duplicate&state=expected",
        "/oauth2/callback?error=access_denied&state=other",
        "/oauth2/callback?code=x&code=y&state=expected",
        "/oauth2/callback?code=x&state=other",
    )
    for callback in rejected:
        try:
            parse_callback(callback, "expected")
        except CalendarConfigurationError:
            pass
        else:
            raise AssertionError("unsafe callback was accepted")
    assert parse_callback("/oauth2/callback?code=x&state=expected", "expected") == "x"


def test_callback_handler_continues_after_invalid_request_then_accepts_valid_callback():
    server = _CallbackServer((
        "/oauth2/callback?error=access_denied",
        "/oauth2/callback?error=access_denied&error=duplicate&state=expected",
        "/oauth2/callback?error=access_denied&state=wrong",
        "/oauth2/callback?code=accepted&state=expected",
    ))
    oauth = CalendarOAuth(_config(), secret_store=_Secrets(), max_callback_requests=4)
    assert oauth._wait_for_callback(server, "expected") == "accepted"
    assert server.responses == [400, 400, 400, 200]


def test_connect_stops_on_valid_denial_without_http_or_secret_writes():
    server = _CallbackServer(())
    requests = []
    store = _Secrets()

    def browser(url):
        state = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)["state"][0]
        server.paths = iter([f"/oauth2/callback?error=access_denied&state={state}"])
        return True

    oauth = CalendarOAuth(_config(), secret_store=store,
                         post_form=lambda *_args: requests.append(True),
                         browser_open=browser, server_factory=lambda *_args: server)
    try:
        oauth.connect()
    except CalendarAuthorizationDeniedError:
        pass
    else:
        raise AssertionError("valid authorization denial was not reported")
    assert not requests and store.token is None and not store.cleared


def test_callback_restores_server_attributes_after_success_and_failure():
    successful = _CallbackServer(("/oauth2/callback?code=accepted&state=expected",))
    successful.timeout = "original"
    original_handler = successful.RequestHandlerClass
    oauth = CalendarOAuth(_config(), secret_store=_Secrets())
    assert oauth._wait_for_callback(successful, "expected") == "accepted"
    assert successful.RequestHandlerClass is original_handler
    assert successful.timeout == "original"

    failed = _PartialServer()
    failed.timeout = "original"
    original_handler = failed.RequestHandlerClass
    original_get_request = failed.get_request.__func__
    try:
        oauth._wait_for_callback(failed, "expected")
    except CalendarUnavailableError:
        pass
    else:
        raise AssertionError("partial callback request was accepted")
    assert failed.RequestHandlerClass is original_handler
    assert failed.timeout == "original"
    assert "get_request" not in vars(failed)
    assert failed.get_request.__func__ is original_get_request


def test_callback_sets_a_bounded_timeout_for_partial_connections():
    server = _PartialServer()
    oauth = CalendarOAuth(_config(), secret_store=_Secrets(), callback_timeout=30,
                         max_callback_requests=1)
    try:
        oauth._wait_for_callback(server, "expected")
    except CalendarUnavailableError:
        pass
    else:
        raise AssertionError("partial callback request was accepted")
    assert 0 < server.request.timeout <= 10


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


def test_oauth_listener_can_bind_all_interfaces_without_advertising_them():
    # Record the configured bind address and the browser-facing authorization URL.
    server = _Server(49153)
    calls = []
    browser_urls = []

    def factory(address, _handler):
        # Keep the listener address separate from the advertised redirect address.
        calls.append(address)
        return server

    # Bind all interfaces for the container while preserving loopback in the redirect URI.
    with patch.dict(os.environ, {"MEETING_RECORDER_GOOGLE_OAUTH_LISTEN_ADDRESS": "0.0.0.0"}):
        oauth = CalendarOAuth(_config(), secret_store=_Secrets(), post_form=lambda *_: _response(),
                              browser_open=lambda url: browser_urls.append(url) or True,
                              server_factory=factory)
        oauth._wait_for_callback = lambda _server, _state: "authorization-code"
        oauth.connect()

    # Confirm Google still receives only the host browser's loopback callback URI.
    assert calls == [("0.0.0.0", 0)]
    redirect_uri = urllib.parse.parse_qs(urllib.parse.urlsplit(browser_urls[0]).query)["redirect_uri"]
    assert redirect_uri == ["http://127.0.0.1:49153/oauth2/callback"]


def test_oauth_listener_address_is_strictly_limited():
    # Accept only the two listener addresses supported by the container deployment.
    assert validate_listener_address("127.0.0.1") == "127.0.0.1"
    assert validate_listener_address("0.0.0.0") == "0.0.0.0"

    # Reject names, IPv6, whitespace, and missing values before listener creation.
    for value in ("localhost", "::1", "0.0.0.1 ", None):
        try:
            validate_listener_address(value)
        except CalendarConfigurationError:
            pass
        else:
            raise AssertionError("unsafe OAuth listener address was accepted")


def test_portal_open_uri_accepts_only_a_success_response_code() -> None:
    """Map bounded portal response values to a simple browser-dispatch result."""
    calls = []

    # Pass the HTTPS URI to the narrow transport seam without requiring GI.
    assert _open_uri_with_portal(
        "https://accounts.google.com/authorize", lambda uri: calls.append(uri) or 0)
    assert calls == ["https://accounts.google.com/authorize"]

    # Treat portal denials, timeouts, malformed values, and transport errors as failures.
    for result in (1, None, "0", True):
        assert not _open_uri_with_portal("https://accounts.google.com/authorize", lambda _uri, result=result: result)
    assert not _open_uri_with_portal(
        "https://accounts.google.com/authorize",
        lambda _uri: (_ for _ in ()).throw(RuntimeError("portal unavailable")),
    )


def test_portal_transport_uses_exact_open_uri_contract_and_matching_response() -> None:
    """Send the fixed portal method call and ignore unrelated Request responses."""
    harness = _PortalHarness()

    # Complete a matching success response after the OpenURI method returns its path.
    result = _portal_open_uri_response("https://accounts.google.com/authorize", gio=harness.gio,
                                       glib=harness.glib)

    # Verify the subscription is pinned to the desktop portal sender before OpenURI runs.
    assert harness.subscription[:5] == (
        "org.freedesktop.portal.Desktop", "org.freedesktop.portal.Request", "Response", None, None)
    open_uri = harness.calls[0]
    assert open_uri[:4] == (
        "org.freedesktop.portal.Desktop", "/org/freedesktop/portal/desktop",
        "org.freedesktop.portal.OpenURI", "OpenURI")
    assert open_uri[4].signature == "(ssa{sv})"
    assert open_uri[4].value == ("", "https://accounts.google.com/authorize", {})
    assert result == 0

    # Confirm the live timeout source and D-Bus subscription are cleaned up after success.
    assert harness.removed_sources == [7]
    assert harness.unsubscribed == [9]


def test_portal_transport_keeps_newest_early_response_and_returns_nonzero() -> None:
    """Retain the matching early signal after unrelated buffered responses fill the queue."""
    harness = _PortalHarness(response=2, early=True)

    # Deliver the matching response during call_sync after older unrelated responses.
    result = _portal_open_uri_response("https://accounts.google.com/authorize", gio=harness.gio,
                                       glib=harness.glib)

    # Return the portal denial without entering the response wait loop.
    assert result == 2
    assert not harness.loop_ran
    assert harness.unsubscribed == [9]


def test_portal_transport_timeout_closes_request_without_stale_source_removal() -> None:
    """Close the exact pending portal request when its bounded response wait expires."""
    harness = _PortalHarness(timeout=True)

    # Let the fake GLib timeout finish the loop without a Request.Response signal.
    result = _portal_open_uri_response("https://accounts.google.com/authorize", gio=harness.gio,
                                       glib=harness.glib)

    # Close only the returned Request object and avoid removing an already-fired source.
    assert result is None
    close = harness.calls[1]
    assert close[:4] == (
        "org.freedesktop.portal.Desktop", harness.request_path,
        "org.freedesktop.portal.Request", "Close")
    assert close[4] is None
    assert harness.removed_sources == []
    assert harness.unsubscribed == [9]


def test_managed_default_uses_portal_and_native_default_uses_webbrowser() -> None:
    """Choose the host portal only for the managed container's default launcher."""
    server = _Server()
    portal_urls = []
    native_urls = []

    # Use the portal in managed mode when the caller did not inject a browser opener.
    with patch.dict(os.environ, {"MEETING_RECORDER_MANAGED_CONTAINER": "1"}), \
            patch("meeting_recorder.calendar_oauth.webbrowser.open", side_effect=native_urls.append):
        managed = CalendarOAuth(
            _config(), secret_store=_Secrets(), post_form=lambda *_: _response(),
            portal_open=lambda uri: portal_urls.append(uri) or True,
            server_factory=lambda *_: server,
        )
        managed._wait_for_callback = lambda _server, _state: "authorization-code"
        managed.connect()

    # Use Python's native browser opener outside managed containers.
    with patch.dict(os.environ, {}, clear=True), \
            patch("meeting_recorder.calendar_oauth.webbrowser.open", side_effect=lambda uri: native_urls.append(uri) or True):
        native = CalendarOAuth(
            _config(), secret_store=_Secrets(), post_form=lambda *_: _response(),
            portal_open=lambda _uri: (_ for _ in ()).throw(AssertionError("portal used natively")),
            server_factory=lambda *_: _Server(),
        )
        native._wait_for_callback = lambda _server, _state: "authorization-code"
        native.connect()

    # Confirm each execution path used exactly its intended browser dispatch mechanism.
    assert len(portal_urls) == len(native_urls) == 1


def test_explicit_browser_opener_wins_over_managed_portal_default() -> None:
    """Keep injected browser openers as the highest-priority deterministic seam."""
    browser_urls = []

    # Preserve the explicit opener even though the process identifies as managed.
    with patch.dict(os.environ, {"MEETING_RECORDER_MANAGED_CONTAINER": "1"}):
        oauth = CalendarOAuth(
            _config(), secret_store=_Secrets(), post_form=lambda *_: _response(),
            browser_open=lambda uri: browser_urls.append(uri) or True,
            portal_open=lambda _uri: (_ for _ in ()).throw(AssertionError("portal overrode browser")),
            server_factory=lambda *_: _Server(),
        )
        oauth._wait_for_callback = lambda _server, _state: "authorization-code"
        oauth.connect()

    # Confirm the injected opener received the authorization request.
    assert len(browser_urls) == 1


def test_portal_dispatch_failure_uses_the_existing_redacted_connect_error() -> None:
    """Expose no portal or authorization details when host browser dispatch fails."""
    server = _Server()

    # Make the managed default fail after listener setup and before callback processing.
    with patch.dict(os.environ, {"MEETING_RECORDER_MANAGED_CONTAINER": "1"}):
        oauth = CalendarOAuth(
            _config(), secret_store=_Secrets(), portal_open=lambda _uri: False,
            server_factory=lambda *_: server,
        )
        try:
            oauth.connect()
        except CalendarUnavailableError as exc:
            message = str(exc)
        else:
            raise AssertionError("portal failure did not stop OAuth")

    # Keep the established generic failure and close the bound listener.
    assert message == "Could not open a browser for Google authorization"
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
    except CalendarUnavailableError:
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
    except CalendarConfigurationError:
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
        except CalendarConfigurationError:
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
    assert transient == CalendarStatus("connected", "credential present; validation unavailable", 1)
    assert token.token == "saved-refresh"
    deficient = CalendarOAuth(_config(), secret_store=token,
                              post_form=lambda *_: _response(scope=SCOPES[0])).status()
    assert deficient.state == "misconfigured"


def test_status_disconnected_and_validation_rules_make_no_google_call():
    calls = []
    status = CalendarOAuth(_config(client=""), secret_store=_Secrets(),
                           post_form=lambda *_: calls.append(True)).status()
    assert status.state == "disconnected" and not calls
    assert CalendarOAuth(_config(port=True), secret_store=_Secrets(),
                         post_form=lambda *_: calls.append(True)).status().state == "misconfigured"
    for invalid in (" client.apps.googleusercontent.com", "https://x.apps.googleusercontent.com",
                    "{\"client_id\":\"x\"}", "x/apps.googleusercontent.com"):
        try:
            validate_client_id(invalid)
        except CalendarConfigurationError:
            pass
        else:
            raise AssertionError("unsafe client ID was accepted")
    for invalid in (True, 1.5, -1, 65536):
        try:
            validate_loopback_port(invalid)
        except CalendarConfigurationError:
            pass
        else:
            raise AssertionError("unsafe port was accepted")


def test_status_preserves_connected_state_for_transient_failures_and_missing_scope():
    token = _Secrets("saved-refresh")
    transient_cases = (
        lambda *_args: (408, b"not json"),
        lambda *_args: (429, b"not json"),
        lambda *_args: (502, b"not json"),
        lambda *_args: (_ for _ in ()).throw(CalendarUnavailableError("offline")),
    )
    for post in transient_cases:
        result = CalendarOAuth(_config(), secret_store=token, post_form=post).status()
        assert result == CalendarStatus("connected", "credential present; validation unavailable", 1)
        assert token.token == "saved-refresh"

    missing_scope = CalendarOAuth(
        _config(), secret_store=token,
        post_form=lambda *_args: (200, b'{"access_token":"short-lived"}')).status()
    assert missing_scope == CalendarStatus("connected")


def test_status_keeps_hidden_credential_misconfigured_without_google_call():
    calls = []
    result = CalendarOAuth(_config(client=""), secret_store=_Secrets("saved-refresh"),
                           post_form=lambda *_args: calls.append(True)).status()
    assert result == CalendarStatus(
        "misconfigured", "credential present but client ID is not configured", 1)
    assert not calls


def test_status_treats_an_explicit_empty_environment_sentinel_as_misconfigured():
    result = CalendarOAuth(_config(client=None), secret_store=_Secrets()).status()
    assert result.state == "misconfigured"


def test_request_token_classifies_invalid_grant_and_rate_limit_403():
    oauth = CalendarOAuth(_config(), secret_store=_Secrets())
    oauth._post_form = lambda *_args: (400, b'{"error":"invalid_grant"}')
    try:
        oauth._request_token({})
    except CalendarExpiredError:
        pass
    else:
        raise AssertionError("invalid_grant did not expire the credential")
    oauth._post_form = lambda *_args: (403, b'{"error":"rate_limit_exceeded"}')
    try:
        oauth._request_token({})
    except CalendarUnavailableError:
        pass
    else:
        raise AssertionError("rate-limited request was not transient")


def test_url_network_failure_is_transient_without_an_http_request():
    with patch("meeting_recorder.calendar_oauth.urllib.request.urlopen",
               side_effect=urllib.error.URLError("offline")):
        try:
            _post_form("https://example.invalid", {}, 1)
        except CalendarUnavailableError:
            pass
        else:
            raise AssertionError("network failure was not transient")


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


def test_disconnect_reports_cache_cleanup_failure_after_attempting_secret_clear():
    store = _Secrets("saved-refresh")

    def cache_fails():
        raise OSError("read-only")

    result = CalendarOAuth(_config(), secret_store=store,
                           cache_clear=cache_fails).disconnect()
    assert result.state == "misconfigured"
    assert result.exit_code == 1
    assert store.cleared and store.token is None


def test_reconnect_clears_prior_cache_before_saving_new_refresh_token_under_shared_operation():
    events = []

    class RecordingSecrets(_Secrets):
        def save(self, token):
            events.append(("save", token))
            super().save(token)

    server = _Server()
    oauth = CalendarOAuth(_config(), secret_store=RecordingSecrets("old"), post_form=lambda *_: _response(),
                          browser_open=lambda _url: True, server_factory=lambda *_: server,
                          cache_clear=lambda: events.append(("cache", None)))
    oauth._wait_for_callback = lambda _server, _state: "authorization-code"
    oauth.connect()
    assert events == [("cache", None), ("save", "new-refresh")]


def test_connect_cache_clear_failure_revokes_new_token_without_saving_and_names_the_failure():
    revoked = []
    store = _Secrets()

    def post(url, data, _timeout):
        if "revoke" in url:
            revoked.append(data)
            return 200, b""
        return _response()

    oauth = CalendarOAuth(_config(), secret_store=store, post_form=post,
                          browser_open=lambda _url: True, server_factory=lambda *_: _Server(),
                          cache_clear=lambda: (_ for _ in ()).throw(OSError("read-only")))
    oauth._wait_for_callback = lambda _server, _state: "authorization-code"
    try:
        oauth.connect()
    except CalendarConfigurationError as exc:
        assert "cache" in str(exc).lower()
    else:
        raise AssertionError("cache failure accepted a new credential")
    assert store.token is None and revoked == [{"token": "new-refresh"}]


def test_disconnect_clears_secret_before_cache_so_late_refresh_has_no_token_to_use():
    events = []

    class OrderedSecrets(_Secrets):
        def clear(self):
            events.append("secret")
            super().clear()

    store = OrderedSecrets("saved-refresh")
    result = CalendarOAuth(_config(), secret_store=store,
                           cache_clear=lambda: events.append("cache")).disconnect()
    assert result.state == "disconnected" and events == ["secret", "cache"] and store.load() is None


def test_shared_operation_lock_makes_disconnect_wait_for_an_active_refresh():
    previous = os.environ.get("XDG_CACHE_HOME")
    with tempfile.TemporaryDirectory() as temporary:
        os.environ["XDG_CACHE_HOME"] = temporary
        entered, release, complete = threading.Event(), threading.Event(), threading.Event()
        events = []

        class Client:
            def list_occurrences(self, _calendar_id, _start, _end, *, cancel=None):
                events.append("refresh")
                entered.set()
                release.wait()
                return ()

        store = _Secrets("saved-refresh")
        oauth = CalendarOAuth(_config(client=""), secret_store=store,
                              cache_clear=lambda: events.append("cache"))
        refresh = threading.Thread(target=lambda: CalendarRefresher(
            Client(), CalendarCache()).refresh(("calendar",), blocking=True))
        disconnect = threading.Thread(target=lambda: (oauth.disconnect(), complete.set()))
        refresh.start()
        assert entered.wait(1)
        disconnect.start()
        assert not complete.is_set()
        release.set()
        refresh.join(1)
        disconnect.join(1)
        assert complete.is_set() and events == ["refresh", "cache"] and store.load() is None
    if previous is None:
        os.environ.pop("XDG_CACHE_HOME", None)
    else:
        os.environ["XDG_CACHE_HOME"] = previous


def test_late_refresh_after_disconnect_cannot_store_without_a_remaining_token():
    previous = os.environ.get("XDG_CACHE_HOME")
    with tempfile.TemporaryDirectory() as temporary:
        os.environ["XDG_CACHE_HOME"] = temporary
        store, requests = _Secrets("saved-refresh"), []
        CalendarOAuth(_config(client=""), secret_store=store, cache_clear=lambda: None).disconnect()

        def access_token():
            if store.load() is None:
                raise CalendarApiError("credential missing", transient=False)
            return "token"

        client = GoogleCalendarClient(access_token, lambda *_args: requests.append(True))
        cache = CalendarCache()
        report = CalendarRefresher(client, cache).refresh(("calendar",), blocking=True)
        assert not report.results[0].success and not requests and cache.load("calendar") is None
    if previous is None:
        os.environ.pop("XDG_CACHE_HOME", None)
    else:
        os.environ["XDG_CACHE_HOME"] = previous


def test_connect_waits_for_active_refresh_lock_then_clears_cache_before_saving_token():
    previous = os.environ.get("XDG_CACHE_HOME")
    with tempfile.TemporaryDirectory() as temporary:
        os.environ["XDG_CACHE_HOME"] = temporary
        entered, release, complete = threading.Event(), threading.Event(), threading.Event()
        events = []

        class Client:
            def list_occurrences(self, _calendar_id, _start, _end, *, cancel=None):
                events.append("refresh")
                entered.set()
                release.wait()
                return ()

        class RecordingSecrets(_Secrets):
            def save(self, token):
                events.append("save")
                super().save(token)

        oauth = CalendarOAuth(_config(), secret_store=RecordingSecrets("old"), post_form=lambda *_: _response(),
                              browser_open=lambda _url: True, server_factory=lambda *_: _Server(),
                              cache_clear=lambda: events.append("cache"))
        oauth._wait_for_callback = lambda _server, _state: "authorization-code"
        refresh = threading.Thread(target=lambda: CalendarRefresher(
            Client(), CalendarCache()).refresh(("calendar",), blocking=True))
        connect = threading.Thread(target=lambda: (oauth.connect(), complete.set()))
        refresh.start()
        assert entered.wait(1)
        connect.start()
        assert not complete.is_set()
        release.set()
        refresh.join(1)
        connect.join(1)
        assert complete.is_set() and events == ["refresh", "cache", "save"]
    if previous is None:
        os.environ.pop("XDG_CACHE_HOME", None)
    else:
        os.environ["XDG_CACHE_HOME"] = previous
