# REAPER surround room feed

> ⚠️ **ALPHA — does not work yet. Do not use for a real performance.**
>
> This surround room feed is part of the experimental **living-room / dual-output
> mix** (orchestra through the LG surround system, optionally alongside the
> Clavinova). It is **not** production-ready: the audio path through the TV →
> eARC → soundbar adds ~120–150 ms of latency that is **not yet compensated**, so
> the orchestra plays audibly late, and the latency calibration is unfinished.
> The rear-channel routing and the Yamaha/room balance are still being worked out.
>
> **The supported live-performance path is the orchestra through the Clavinova's
> own synth** (a single MIDI output, near-zero latency). Everything in this folder,
> plus the `room_center` REAPER zone and the dual-output (Yamaha + room) mix, is
> alpha and opt-in — enabling the REAPER renderer is what turns it on. Leave it off
> for real playing. See the status note in
> [`docs/runbooks/reaper-orchestra-host.md`](../../docs/runbooks/reaper-orchestra-host.md).

Fills the living-room LG system (soundbar + 2 rear satellites + subwoofer) from
Rubato's **stereo** orchestra mix, by upmixing on REAPER's master and sending
discrete channels out over HDMI/eARC.

## What we verified

- The Mac negotiates **8 LPCM channels** to `LG TV SSCR2` over HDMI; the TV
  passes them to the bar via **eARC**. Discrete multichannel PCM is accepted
  (confirmed by the subwoofer responding to channel 4 alone) — no Dolby/DTS
  encode needed.
- Rubato's mix is stereo, so untouched it only feeds front L/R (ch 1/2); the
  center, LFE (sub), and rear satellites sit silent. Hence the upmix.
- The subwoofer needed a level boost to be clearly audible.
- The bar must be awake/connected; surround content is fed to **both** the side
  (5/6) and rear-back (7/8) pairs so whichever the physical rears use will play.

## Files

- `rubato_upmix_51.jsfx` — the upmix: fronts pass through, center = mono,
  LFE = ~120 Hz low-passed mono (subwoofer, +boost), surround = L/R copied to
  ch 5/6 **and** 7/8. Install to
  `~/Library/Application Support/REAPER/Effects/`.
- `rubato_surround_setup.lua` — one-shot: sets the Master to 8 channels,
  (re)inserts the upmix, and routes master channels 3–8 to hardware outputs 3–8.
  Install to `~/Library/Application Support/REAPER/Scripts/Rubato/` and run once
  from the Actions list. Idempotent; re-run after editing the JSFX.

## Apply / adjust

1. Copy both files to the REAPER locations above.
2. In REAPER: Actions → New action → Load ReaScript → `rubato_surround_setup.lua`
   → Run.
3. To change balance, edit the JSFX sliders (Surround / Center / Subwoofer dB) on
   the master, or edit `rubato_upmix_51.jsfx` and re-run the setup script.

The config is stored in the machine-local `Rubato Orchestra.RPP`, so live
performance plays in surround automatically once applied.

## Caveats

- This is a **room-filling upmix**, not a discrete spatial mix — every section
  is spread to all speakers, not placed (violins-front, etc.). Per-section
  placement is a later step if wanted.
- Changing the audio device (replugging the bar, editing master channels) makes
  the renderer briefly fail and, currently, can spin in a retry loop; a backend
  restart clears it. Hardening that device-change recovery is a follow-up.
