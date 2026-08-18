"""Desktop prompt behavior without a notification daemon."""

from meeting_recorder import notifier as notifier_module
from meeting_recorder.notifier import Notifier


class FakeNotification:
    emit_close_signal = True

    def __init__(self, summary, body, icon):
        self.summary = summary
        self.body = body
        self.icon = icon
        self.actions = {}
        self.signals = {}
        self.shown = False
        self.close_count = 0

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
        self.close_count += 1
        if self.emit_close_signal:
            self.signals["closed"](self)


class FakeNotify:
    notes = []
    caps = ["actions"]

    class Urgency:
        CRITICAL = "critical"

    class Notification:
        @staticmethod
        def new(summary, body, icon):
            note = FakeNotification(summary, body, icon)
            FakeNotify.notes.append(note)
            return note

    @classmethod
    def get_server_caps(cls):
        return cls.caps


class FakeGLib:
    next_source = 1
    callbacks = {}
    removed = []

    @classmethod
    def timeout_add(cls, milliseconds, callback):
        source = cls.next_source
        cls.next_source += 1
        cls.callbacks[source] = (milliseconds, callback)
        return source

    @classmethod
    def source_remove(cls, source):
        cls.removed.append(source)
        cls.callbacks.pop(source, None)

    @classmethod
    def fire(cls, source):
        _milliseconds, callback = cls.callbacks.pop(source)
        return callback()


def _ready_notifier():
    notifier = object.__new__(Notifier)
    notifier._ready = True
    notifier._active = None
    notifier._prompt_generation = 0
    notifier._prompt_timer_source = None
    notifier._live = set()
    return notifier


def _install_fakes():
    previous = (notifier_module.Notify, notifier_module.GLib)
    notifier_module.Notify = FakeNotify
    notifier_module.GLib = FakeGLib
    FakeNotify.notes = []
    FakeNotify.caps = ["actions"]
    FakeNotification.emit_close_signal = True
    FakeGLib.next_source = 1
    FakeGLib.callbacks = {}
    FakeGLib.removed = []
    return previous


def _restore_fakes(previous):
    notifier_module.Notify, notifier_module.GLib = previous


def _prompt(notifier, events, prefix=""):
    notifier.prompt_capture_mode(
        "Meet", 5,
        lambda: events.append(f"{prefix}video"),
        lambda: events.append(f"{prefix}audio"),
        lambda: events.append(f"{prefix}ignore"),
    )
    return FakeNotify.notes[-1]


def test_capture_prompt_exposes_direct_video_audio_only_and_ignore_actions():
    previous = _install_fakes()
    events = []

    try:
        notifier = _ready_notifier()
        notifier.prompt_capture_mode("Zoom", 30,
                                     lambda: events.append("video"),
                                     lambda: events.append("audio"),
                                     lambda: events.append("ignore"))
        note = FakeNotify.notes[-1]
    finally:
        _restore_fakes(previous)

    visible_actions = {key: value[0] for key, value in note.actions.items()
                       if key != "default"}
    assert note.summary == "Meeting detected"
    assert note.body == "A Zoom call is in progress. Choose what to capture."
    assert visible_actions == {
        "video": "Video", "audio-only": "Audio only", "ignore": "Ignore",
    }
    assert note.actions["default"][0] == ""
    assert note.shown
    assert events == []


def test_capture_prompt_action_closes_and_reentrant_close_dispatches_once():
    previous = _install_fakes()
    events = []

    try:
        notifier = _ready_notifier()
        note = _prompt(notifier, events)
        timer_source = notifier._prompt_timer_source
        note.actions["video"][1](note, "video")
        note.signals["closed"](note)
    finally:
        _restore_fakes(previous)

    assert events == ["video"]
    assert note.close_count == 1
    assert timer_source in FakeGLib.removed
    assert notifier._active is None


def test_capture_prompt_close_without_an_action_ignores():
    previous = _install_fakes()
    events = []

    try:
        notifier = _ready_notifier()
        note = _prompt(notifier, events)
        note.signals["closed"](note)
    finally:
        _restore_fakes(previous)

    assert events == ["ignore"]


def test_capture_prompt_timeout_ignores_and_closes_without_sleeping():
    previous = _install_fakes()
    events = []

    try:
        notifier = _ready_notifier()
        note = _prompt(notifier, events)
        timer_source = notifier._prompt_timer_source
        keep_timer = FakeGLib.fire(timer_source)
    finally:
        _restore_fakes(previous)

    assert keep_timer is False
    assert events == ["ignore"]
    assert note.close_count == 1
    assert notifier._active is None


def test_capture_prompt_default_and_unknown_actions_ignore():
    previous = _install_fakes()

    try:
        outcomes = []
        for action_key, delivered_id in (("default", "default"),
                                         ("video", "unexpected")):
            events = []
            notifier = _ready_notifier()
            note = _prompt(notifier, events)
            note.actions[action_key][1](note, delivered_id)
            outcomes.append(events)
    finally:
        _restore_fakes(previous)

    assert outcomes == [["ignore"], ["ignore"]]


def test_stale_prompt_events_cannot_complete_a_newer_prompt():
    previous = _install_fakes()
    FakeNotification.emit_close_signal = False
    events = []

    try:
        notifier = _ready_notifier()
        old_note = _prompt(notifier, events, "old-")
        old_timeout = FakeGLib.callbacks[notifier._prompt_timer_source][1]
        new_note = _prompt(notifier, events, "new-")
        new_timer_source = notifier._prompt_timer_source

        old_note.signals["closed"](old_note)
        old_note.actions["video"][1](old_note, "video")
        old_timeout()
        assert events == []
        assert notifier._active is new_note
        assert notifier._prompt_timer_source == new_timer_source

        new_note.actions["audio-only"][1](new_note, "audio-only")
    finally:
        _restore_fakes(previous)

    assert events == ["new-audio"]
    assert new_note.close_count == 1
    assert notifier._active is None


def test_capture_prompt_without_action_support_ignores_immediately():
    previous = _install_fakes()
    FakeNotify.caps = []
    events = []

    try:
        notifier = _ready_notifier()
        notifier._fallback = lambda summary, body: None
        notifier.prompt_capture_mode(
            "Meet", 5,
            lambda: events.append("video"),
            lambda: events.append("audio"),
            lambda: events.append("ignore"),
        )
    finally:
        _restore_fakes(previous)

    assert events == ["ignore"]
    assert notifier._active is None
    assert FakeGLib.callbacks == {}
