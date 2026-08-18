"""Desktop prompt behavior without a notification daemon."""

from meeting_recorder import notifier as notifier_module
from meeting_recorder.notifier import Notifier


class FakeNotification:
    def __init__(self, summary, body, icon):
        self.summary = summary
        self.body = body
        self.icon = icon
        self.actions = {}
        self.signals = {}
        self.shown = False

    def set_urgency(self, urgency):
        self.urgency = urgency

    def set_timeout(self, timeout):
        self.timeout = timeout

    def add_action(self, action_id, label, callback):
        self.actions[action_id] = (label, callback)

    def connect(self, signal, callback):
        self.signals[signal] = callback

    def show(self):
        self.shown = True

    def close(self):
        self.signals["closed"](self)


class FakeNotify:
    note = None

    class Urgency:
        CRITICAL = "critical"

    class Notification:
        @staticmethod
        def new(summary, body, icon):
            FakeNotify.note = FakeNotification(summary, body, icon)
            return FakeNotify.note

    caps = ["actions"]

    @classmethod
    def get_server_caps(cls):
        return cls.caps


def _ready_notifier():
    notifier = object.__new__(Notifier)
    notifier._ready = True
    notifier._active = None
    notifier._live = set()
    return notifier


def _install_fake_notify():
    previous = getattr(notifier_module, "Notify", None)
    notifier_module.Notify = FakeNotify
    return previous


def _restore_notify(previous):
    if previous is None:
        del notifier_module.Notify
    else:
        notifier_module.Notify = previous


def test_capture_prompt_exposes_direct_video_audio_only_and_ignore_actions():
    previous_notify = _install_fake_notify()
    events = []

    try:
        notifier = _ready_notifier()
        notifier.prompt_capture_mode("Zoom", 30,
                                     lambda: events.append("video"),
                                     lambda: events.append("audio"),
                                     lambda: events.append("ignore"))
        note = FakeNotify.note
    finally:
        _restore_notify(previous_notify)

    assert note.summary == "Meeting detected"
    assert note.body == "A Zoom call is in progress. Choose what to capture."
    assert {key: value[0] for key, value in note.actions.items()} == {
        "video": "Video", "audio-only": "Audio only", "ignore": "Ignore",
    }
    assert note.shown
    assert events == []


def test_capture_prompt_action_and_close_dispatch_only_once():
    previous_notify = _install_fake_notify()
    events = []

    try:
        notifier = _ready_notifier()
        notifier.prompt_capture_mode("Meet", 30,
                                     lambda: events.append("video"),
                                     lambda: events.append("audio"),
                                     lambda: events.append("ignore"))
        note = FakeNotify.note
        note.actions["video"][1](note, "video")
        note.signals["closed"](note)
    finally:
        _restore_notify(previous_notify)

    assert events == ["video"]
    assert notifier._active is None


def test_capture_prompt_close_without_an_action_ignores():
    previous_notify = _install_fake_notify()
    events = []

    try:
        notifier = _ready_notifier()
        notifier.prompt_capture_mode("Meet", 5,
                                     lambda: events.append("video"),
                                     lambda: events.append("audio"),
                                     lambda: events.append("ignore"))
        FakeNotify.note.signals["closed"](FakeNotify.note)
    finally:
        _restore_notify(previous_notify)

    assert events == ["ignore"]


def test_capture_prompt_without_action_support_ignores_immediately():
    previous_notify = _install_fake_notify()
    previous_caps = FakeNotify.caps
    FakeNotify.caps = []
    events = []

    try:
        notifier = _ready_notifier()
        notifier._fallback = lambda *_args: None
        notifier.prompt_capture_mode("Meet", 5,
                                     lambda: events.append("video"),
                                     lambda: events.append("audio"),
                                     lambda: events.append("ignore"))
    finally:
        FakeNotify.caps = previous_caps
        _restore_notify(previous_notify)

    assert events == ["ignore"]
    assert notifier._active is None
