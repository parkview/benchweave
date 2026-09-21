# Synthetic control protocol

The EMeet C960 is a plain UVC device with no vendor text protocol. This file
describes a **hypothetical** text control channel, used only to exercise the
adapter contract without hardware. No real device is claimed.

- `ID?` + LF → `EMeet,SmartCam C960 4K` + LF (serial/firmware are unknown).
- `GET <control>` + LF → the control's current value as text + LF (int as decimal,
  bool as `0`/`1`, enum as its label).
- `SET <control>=<value>` + LF → `OK` + LF (value already range-checked).

Real control happens through V4L2 (`v4l2-ctl`); capture through `ffmpeg`. See
`docs/eMeet-4K.md` for the device facts these synthetic exchanges are traced to.
