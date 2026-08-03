# Building a Local Scanner Alert System: SDR + AI Transcription

A guide for building a personal alerting pipeline that listens to a local
public-safety radio system via SDR, transcribes relevant traffic with
Whisper, filters it with a local LLM, and sends a push notification when
something relevant is actually happening. This documents the full build —
including the debugging path for a specific class of problem (frequency
offset breaking digital voice while leaving control-channel signaling
intact) that's easy to lose hours to if you don't know to look for it.

## Before you start: research your own system

Every trunked radio system has its own frequencies, protocol variant, and
site layout. Before buying anything, find yours:

1. **RadioReference** (radioreference.com) has a database of public-safety
   trunked systems by county/region. Look up your local system's:
   - System ID (sysid) and WACN
   - Protocol (P25 Phase I, P25 Phase II/TDMA, SmartNet, DMR, etc. — this
     matters a lot, see below)
   - Site list, if the system uses simulcast (multiple transmitters on the
     same frequencies) — note which site covers your location
   - Control channel frequencies for that site
   - The full talkgroup list, ideally with names/descriptions
2. **OpenMHz** (openmhz.com) or **Broadcastify Calls** — if a system similar
   to yours is already being shared by another hobbyist, browsing their feed
   is a great way to confirm you have the right system/site and to see
   real talkgroup activity before you own any hardware. (Programmatic
   access to either is generally blocked by bot-detection; use them for
   browsing/research, not for building an ingestion pipeline against.)
3. **Check the protocol carefully.** P25 Phase II (TDMA) is more sensitive
   to frequency accuracy than Phase I (FDMA) — this matters a lot for the
   debugging section below. If your system uses Phase II, budget extra
   patience for tuning.

## Hardware

- **SDR**: needs enough instantaneous bandwidth to cover your system's full
  control + voice channel range in one capture, so you don't need multiple
  physical radios. Check the span between your lowest and highest channel
  frequencies, then pick an SDR whose sample rate comfortably exceeds that
  (roughly 2x the span, to leave margin — SDR bandwidth has real rolloff
  near the edges). Common choices: RTL-SDR (cheapest, ~2.4MHz usable,
  fine only for narrow systems), Airspy Mini/R2 (3M/6M/10M depending on
  model, good middle ground), SDRplay (up to 10MHz, pricier).
- **Antenna**: match it to your system's frequency band. For any given
  frequency, quarter-wave is `299.8 / (4 x frequency_MHz)` meters —
  e.g. for 850 MHz that's about 8.8cm per element. A telescoping antenna
  sized for a different band (e.g. general VHF/UHF scanner antennas) may
  be a poor match even though it physically fits — check the actual
  element length against your system's frequency, and get the polarization
  right (US public-safety UHF/700-800MHz is vertically polarized: elements
  in a straight vertical line, not folded or angled).
- **Host machine**: a small dedicated PC works well and doesn't need to be
  powerful — a repurposed office mini-desktop (Dell OptiPlex Micro,
  Lenovo Tiny, etc.) or even a Raspberry Pi 4 is plenty for one site.
  x86 is generally easier than ARM for this stack (more mature package
  support), but ARM works too.
  - **USB note**: if the SDR doesn't enumerate (doesn't show up in
    `lsusb`), try a different physical USB port before assuming a
    hardware fault — some ports (especially front-panel ones on desktop
    towers) don't supply enough power/bandwidth for these devices.

## Software stack

- **trunk-recorder** — the standard open-source tool for this. See the
  dedicated install/config section below.
- **GNU Radio + gr-osmosdr** — installed as part of the above.
- **OP25** (boatbod fork on GitHub) — not part of the running pipeline, but
  extremely useful as an independent diagnostic decoder when trunk-recorder
  isn't behaving as expected (see below). Its web terminal (`-l
  http:0.0.0.0:PORT`) shows live frequency error, symbol error rate, and a
  constellation plot.
- **Whisper** (faster-whisper is a good, fast implementation) for
  transcription — run on GPU if you have one available.
- **A local or API LLM** for filtering — the goal is to only alert on
  transcripts that actually describe something relevant near you, not
  every transmission that happens to mention a nearby street name.
- **Pushover** (or similar) for the actual notification.

## Installing trunk-recorder

These steps are for Ubuntu; adapt package names for other distros (the
project's own `docs/Install/` folder has per-OS instructions, but package
lists there can lag a release or two behind the newest Ubuntu — the
closest older version's list usually still works fine).

```bash
# Check your Ubuntu version first -- pick the closest matching package
# list from trunk-recorder's own install docs if yours isn't listed
lsb_release -a

# Core build dependencies
sudo apt-get install -y apt-transport-https build-essential ca-certificates \
  ffmpeg git gnupg gnuradio gnuradio-dev gr-osmosdr libuhd-dev \
  libboost-all-dev libcurl4-openssl-dev libgmp-dev libhackrf-dev \
  liborc-0.4-dev libpthread-stubs0-dev libssl-dev libusb-dev pkg-config \
  software-properties-common cmake libsndfile1-dev

# SDR-specific packages -- install whichever matches your hardware.
# Airspy specifically is easy to miss since some install guides only
# mention HackRF/RTL-SDR by name:
sudo apt install -y airspy libairspy-dev libairspy0
# (swap in the equivalent rtl-sdr / hackrf / sdrplay packages if using
# different hardware)

# Build
mkdir trunk-build
git clone https://github.com/TrunkRecorder/trunk-recorder.git
cd trunk-build
cmake ../trunk-recorder
make
sudo make install
```

**Verify the build actually picked up your SDR's backend.** Trunk-recorder
links against a pre-built `gr-osmosdr` rather than checking SDR support
itself at its own configure step, so a clean `cmake`/`make` doesn't
guarantee your hardware is supported — confirm directly:

```bash
which trunk-recorder
trunk-recorder --version

# For Airspy, confirm the library actually links against it:
ldd /usr/lib/x86_64-linux-gnu/libgnuradio-osmosdr.so | grep -i airspy
# should show libairspy.so.0 -- if it prints nothing, gr-osmosdr wasn't
# built with Airspy support and you may need to build it from source
# with that backend explicitly enabled
```

You can do all of the above with no SDR plugged in yet — it's a pure
software build. Once your hardware arrives, sanity-check it at the
USB/driver level before touching trunk-recorder at all:

```bash
lsusb                    # confirm the OS sees the device
airspy_info               # (or the equivalent tool for your SDR) --
                          # opens the device directly and prints its
                          # serial number/firmware/supported sample
                          # rates; works fine with no antenna attached
```

## Configuring trunk-recorder

Create `config.json` and a talkgroup CSV in the same directory you'll run
`trunk-recorder` from. Current config format (v2) puts `modulation` in the
`systems` block, not `sources`:

```json
{
  "ver": 2,
  "sources": [
    {
      "center": 852728125,
      "rate": 6000000,
      "error": 0,
      "ppm": 0,
      "gain": 20,
      "digitalRecorders": 8,
      "driver": "osmosdr",
      "device": "airspy=0"
    }
  ],
  "systems": [
    {
      "control_channels": [853050000, 853662500, 853775000, 853912500],
      "type": "p25",
      "modulation": "qpsk",
      "squelch": -65,
      "talkgroupsFile": "talkgroups.csv",
      "recordUnknown": false,
      "shortName": "my_system",
      "callLog": true,
      "hideEncrypted": false,
      "hideUnknownTalkgroups": false
    }
  ],
  "defaultMode": "digital",
  "captureDir": "audio_files",
  "callTimeout": 3,
  "logFile": true
}
```

Your talkgroup CSV needs this header row, then one line per talkgroup you
want named/recorded:

```csv
Decimal,Hex,Alpha Tag,Mode,Description,Tag,Category,Priority
```

`Decimal` is the actual over-the-air talkgroup ID (matches what
RadioReference lists), `Hex` is just the same value in hex (informational,
not required for matching), and `Priority` controls which calls win
contention when multiple simultaneous calls compete for a limited number
of recorders (lower number = higher priority — worth setting your primary
interest, e.g. fire/EMS, below less-important traffic if you're recording
more than one agency).

Run it:

```bash
cd wherever-your-config-and-csv-are
trunk-recorder --config=config.json
```

Watch the console output for:
- A rising, non-zero **"Control Channel Message Decode Rate"** — confirms
  the SDR is receiving your system at all
- A line reporting the system's WACN/system ID and site — confirms you've
  actually tuned to the right system, not a neighboring one
- Once a call happens on a talkgroup in your CSV: a "Starting P25
  Recorder" line, followed by "Concluding Recorded Call." A genuinely
  successful call does **not** end in "No Transmissions were recorded!" —
  if every single call ends with that message despite otherwise-correct
  behavior, see the debugging section below before assuming anything else
  is wrong.

Field-by-field notes on what to actually tune (as opposed to copy-paste):

- **`center`**: pick a frequency that puts your system's full channel range
  within the SDR's sample rate, with margin on both edges (don't put it at
  the exact midpoint if you can find a real working config for your
  specific system/site from someone else — there can be legitimate reasons
  a slightly different center works better).
- **`rate`**: your SDR's sample rate; must be at least ~1.25x your total
  channel span, more is safer.
- **`gain`**: start with whatever the SDR's documentation suggests, but
  **treat it as a real variable to tune, not a fixed value** — both too low
  (weak signal) and too high (front-end overload/clipping) can degrade
  decode quality, and the failure mode looks similar from the logs alone.
- **`error`**: frequency correction in Hz. **This is the single most
  important field if you hit the specific failure pattern below.**
- **`ppm`**: an alternative way to express frequency correction, as parts
  per million rather than a fixed Hz offset.
- **`squelch`**: threshold in dB for when a channel is considered to have
  signal. Values around -60 to -70 are common starting points; the
  project's own `docs/SQUELCH.md` explains a useful diagnostic (temporarily
  set it to something extremely permissive like -100 to rule squelch in
  or out as a cause of missing audio).
- **`modulation`**: `qpsk` for P25 Phase II, other values for other
  protocols — get this wrong and your control channel typically won't
  decode at all, which is at least an obvious, fast-failing signal rather
  than a subtle one.
- **`talkgroupsFile`**: a CSV mapping talkgroup IDs to names. If a
  community-shared archive (OpenMHz, Broadcastify) already covers your
  system, their talkgroup list (visible in their web UI or via any public
  API endpoint they expose) is a much faster starting point than manually
  guessing names from raw IDs — cross-reference against the official
  RadioReference list where possible, since community sources can have
  gaps.
- **`recordUnknown`**: set `false` and only list the talkgroups you
  actually want (e.g. just fire/EMS) to keep the alerting pipeline focused.

## The debugging path (read this before you spend hours confused)

The single most valuable thing to know going in: **it's possible for a
trunked-radio SDR setup to receive and decode the control channel perfectly
— correct system ID, correct site, accurate real-time call tracking — while
producing *zero* actual voice audio on every single call**, with no
error message more specific than something like "no transmissions were
recorded." This is a genuinely confusing failure mode because everything
*looks* like it's working: talkgroup IDs resolve correctly, call durations
match real elapsed time, the right recorder gets allocated. It's easy to
assume the problem is squelch, gain, or hardware, and burn a long time
tuning things that aren't the actual cause.

**The likely explanation, if you hit this**: an uncorrected frequency
offset on the SDR. Control channel signaling uses low symbol rates and
heavy forward error correction specifically because it's safety-critical,
so it tolerates a real frequency error that digital voice — especially
Phase II's higher-rate TDMA — cannot. The result is exactly the confusing
pattern above: the control channel "just works" while voice never does,
regardless of how you tune squelch or gain.

**How to find and fix it**:
1. Run an independent decoder (OP25) against your known-working control
   channel frequency. Its web terminal reports a `Frequency error` reading
   directly — this is often measured in Hz and can be a surprisingly large
   value (thousands of Hz, several ppm) even when everything else looks
   fine.
2. Set that value in trunk-recorder's `error` field. **Verify the sign is
   correct** by watching the live control-channel decode rate: the correct
   sign should hold or improve it; the wrong sign will visibly degrade or
   kill it entirely. Don't assume the sign — test it.
3. Trunk-recorder's own `autoTune` feature is supposed to correct this
   automatically over time, but in practice it may never detect an offset
   on its own (reporting `+0 Hz` indefinitely) — don't rely on it alone;
   set the explicit value first, and let autoTune refine from there.

**Other things worth checking, in rough order of likelihood, if the above
doesn't fully explain it**:
- **CPU headroom** — if the decode workload (control channel + several
  simultaneous digital recorders) exceeds what the host can process in
  real time, samples get dropped. Check with `top` while the pipeline is
  running; if a single process is pegged near the CPU core count x 100%,
  that's the bottleneck, not RF.
- **Temp storage** — trunk-recorder stages in-progress recordings
  somewhere (often `/dev/shm`, a RAM-backed filesystem) before finalizing.
  If that's full or too small, writes can fail silently. `df -h` on that
  path.
- **A non-voice talkgroup masquerading as a failure** — some talkgroup IDs
  are data/telemetry channels, not voice, and will legitimately never
  produce audio no matter how well-tuned the system is. A channel firing
  on a suspiciously exact, regular interval (e.g. every 10 seconds) is a
  strong hint it's not a person talking.
- **Simulcast distortion** — if your site combines multiple transmitters
  on the same frequencies, overlapping signals with slightly different
  arrival times can interfere destructively. This has the same
  "control-survives-voice-fails" signature as frequency offset, so rule
  out frequency error first; if that doesn't fix it, a directional antenna
  aimed to favor one transmitter is the usual remedy.
- **Antenna mismatch** — see the hardware section; a wrong length or wrong
  polarization can produce a link that's just barely good enough for the
  control channel and not for voice.

## Why SDR over a cloud/API-based approach

If a paid API exists for your system's radio traffic, it's worth comparing
cost against hardware. A one-time SDR + antenna purchase (on the order of
$100-150) becomes cheaper than an ongoing subscription surprisingly fast,
especially for busier systems, and it removes any dependency on a third
party's pricing, terms of service, or infrastructure decisions going
forward. Free community-shared feeds (OpenMHz, etc.) are useful for
research but are typically protected against programmatic/bot access, so
they're not a reliable foundation to build an automated pipeline on top of
— treat them as a way to confirm your target system before buying
hardware, not as a data source for the finished project.

## Open items / general next steps

- Confirm your tuned gain/frequency-correction values hold up over a long
  unattended run, not just a short test.
- Wire the recorder's output directory into your Whisper -> LLM ->
  notification pipeline.
- Build out your talkgroup CSV as you learn what's actually on your
  system — start narrow (e.g. just the agency you care about) and expand
  from real logged activity rather than guessing up front.
