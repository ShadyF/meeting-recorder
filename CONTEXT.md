# Meeting Recorder

The context covers consented meeting capture, enrichment, and optional publication while preserving standalone recording.

## Language

**Recording**:
A finalized local media file produced by a completed capture.
_Avoid_: Capture file, output file

**Completed Recording**:
An immutable description of a successfully finalized Recording and its optional
Meeting metadata snapshot. Enrichment returns a replacement value; it never
mutates the finalized result or media after dispatch.
_Avoid_: Recording result, completion event

**Meeting metadata**:
Calendar-derived identity for a recording, including its title, participants, scheduled time, description, and location.
_Avoid_: Calendar data, recording details

**Calendar match**:
The deterministic association between one Recording interval and one Google Calendar event instance.
_Avoid_: Event guess, calendar lookup

**Meeting sidecar**:
A versioned `<media filename>.meeting.json` file adjacent to a Recording. Schema
version 1 stores the capture interval, original fallback filename, stable
occurrence selector, and current Meeting metadata. It is written atomically;
the Recording remains authoritative if sidecar or rename work fails.
_Avoid_: Metadata file, recording JSON

**Recording correction**:
An explicit cache-only operation under `meeting-recorder calendar correct` that
lists nearby fresh occurrences, selects one exact stable selector, or clears a
sidecar. It never performs a network refresh unless `--refresh` is requested and
never changes old recordings automatically after a later cache refresh.

**Capture mode**:
The selected media composition for a recording: audio-only or audio-video.
_Avoid_: Recording type, media mode

**Video source**:
The selected screen content for video capture: fullscreen, window, or area.
_Avoid_: Capture mode, screen mode

**Speakr publisher**:
The optional, explicitly invoked component that uploads a Recording and its
selected public metadata to Speakr. A visible matched Meeting supplies current
title/details and scheduled start; a hidden matched Meeting supplies the
current filename title and non-private scheduled start without private
notes/participants; an unmatched Recording with a valid sidecar supplies the
current filename title and capture start; without a valid sidecar, file mtime is
the date fallback. The current path and sidecar are reread after transfer before
the authoritative metadata PATCH.
_Avoid_: Speakr plugin, Speakr connector

**Publication job**:
The durable public-only record of one Recording's progress toward publication in
Speakr. It contains no credentials or private Meeting metadata; uncertain media
transfers are not automatically resent, metadata-pending jobs retry PATCH only,
and published reruns send nothing.
_Avoid_: Upload record, queue item
