# LG S95-Series HDMI Latency

- Checked: 2026-08-03
- Role: evidence and explicit MVP assumption for the room-center zone

## Model Caveat

The soloist referred to an LG `S95A` soundbar. Current retail references commonly use
`S95AR` for LG's 2025 flagship, while the accessible numerical benchmark is for
the earlier `S95QR`. Confirm the exact rear-label model before writing a
hardware profile.

## Evidence

- LG documents regular HDMI inputs, HDMI output, LPCM, a Game sound mode, and
  ALLM/VRR passthrough across the relevant flagship line.
- The accessible Audio Gurus S95QR test summary reports 59 ms for full HDMI
  input, 86 ms optical, and 131 ms ARC.
- RTINGS has format-specific HDMI/ARC/optical latency measurements for S95QR and
  S95AR, but the exact current tables are membership-gated. Its public review
  confirms the measurements exist; it does not expose a reusable S95AR number.
- Tom's Guide and TechRadar confirm gaming modes and ALLM passthrough but do not
  publish an audio-onset benchmark for Game versus Standard.

## Accepted Project Assumption

The soloist accepts 59 ms as the planning value for direct-HDMI Game-mode design. This
is sufficient to design the route inside Rubato's normal 100 ms dispatch
horizon. It is not a claim that the complete installed path has already been
measured.

Performance readiness still requires an end-to-end bench measurement including
BBCSO/Pedalboard, CoreAudio buffers, HDMI, soundbar DSP, wireless speaker
synchronization, and acoustic propagation to the pianist.

## Sources

- [LG S95QR user guide](https://www.lg.com/us/support/help-library/lg-sound-bar-s95qr-users-guide--20153203745245)
- [LG S95QR product page](https://www.lg.com/us/sound-bars/lg-s95qr-sound-bar)
- [LG S95AR product page](https://www.lg.com/us/soundbars/lg-s95ar-soundbar)
- [RTINGS S95QR review](https://www.rtings.com/soundbar/reviews/lg/s95qr)
- [RTINGS S95AR review](https://www.rtings.com/soundbar/reviews/lg/s95ar)
- [Audio Gurus S95QR latency summary](https://www.audiogurus.com/review/LG-S95QR-9-1-5-ch-High-Res-Audio-Soundbar)
- [Tom's Guide S95QR review](https://www.tomsguide.com/reviews/lg-s95qr-review)

## Related

- [Calibrated Low-Latency Spatial Mixing](../concepts/psychoacoustic-spatial-mixing.md)
- [Decision 0016](../decisions/0016-pedalboard-decoupled-spatial-synth.md)
- [BBCSO Secondary Audio-Zone Audition](../runbooks/bbcso-audio-zone.md)
