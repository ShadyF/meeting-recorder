# Meeting Recorder

The context covers consented meeting capture, enrichment, and optional publication while preserving standalone recording.

## Language

**Recording**:
A finalized local media file produced by a completed capture.
_Avoid_: Capture file, output file

**Completed Recording**:
An immutable description of a successfully finalized Recording and its optional Meeting metadata snapshot.
_Avoid_: Recording result, completion event

**Meeting metadata**:
Calendar-derived identity for a recording, including its title, participants, scheduled time, description, and location.
_Avoid_: Calendar data, recording details

**Calendar match**:
The deterministic association between one Recording interval and one Google Calendar event instance.
_Avoid_: Event guess, calendar lookup

**Meeting sidecar**:
A versioned file adjacent to a Recording that stores its current Calendar match and Meeting metadata.
_Avoid_: Metadata file, recording JSON

**Capture mode**:
The selected media composition for a recording: audio-only or audio-video.
_Avoid_: Recording type, media mode

**Video source**:
The selected screen content for video capture: fullscreen, window, or area.
_Avoid_: Capture mode, screen mode

**Speakr publisher**:
The optional component that uploads a recording and its meeting metadata to Speakr.
_Avoid_: Speakr plugin, Speakr connector

**Publication job**:
The durable record of one Recording's progress toward publication in Speakr.
_Avoid_: Upload record, queue item
