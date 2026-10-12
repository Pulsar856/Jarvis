# Local audio operations

Sprint 2 adds explicit, bounded local audio to Jarvis v0.3.0. `status`, `demo`,
and `run` retain their existing behavior. Importing modules, starting the core,
or starting the systemd service does not open a microphone. Audio commands need
no network connection, account, credential, or Python audio dependency.

These WAV commands are preserved in v0.4.0. Sprint 3 adds separate raw PCM
operations documented in [audio streaming](audio-streaming.md); the format and
file-safety rules below still describe the WAV subsystem.

## Audited Raspberry Pi environment

The audit on 12 October 2026 found Debian 12 ARM64, Python 3.11.2, ALSA
`arecord`/`aplay` 1.2.8, PipeWire 1.2.7, and an active PipeWire PulseAudio
compatibility server. The user session's default source and sink were the Jabra.
Jarvis selects named ALSA hardware explicitly rather than using those defaults.

| Observed property | Value |
| --- | --- |
| Device | Jabra SPEAK 510 USB |
| USB vendor/product | `0b0e:0422` |
| ALSA card/device at audit | Card 2, card name `USB`, device 0 |
| Other cards | `vc4hdmi0` and `vc4hdmi1`, playback only |
| Native capture | S16_LE, mono, 16000 Hz |
| Advertised native playback | S16_LE, stereo, 8000 / 16000 / 48000 Hz |
| USB control identity | `usb-0b0e_Jabra_SPEAK_510_USB_08C8C2AAA158022000-00` |
| Required system packages | Already installed: `alsa-utils`, `libasound2-plugins` |

These are observations from this Pi, not assumptions for other Jabra models.
Card numbers can change after boot or reconnection. Product code does not use
the observed card number as a persistent identity.

The `user` account belongs to the `audio` group and had read/write access to
`/dev/snd`. The running system service has that supplementary group and no
device-isolation restriction. The SSH session has `XDG_RUNTIME_DIR` and an
available PulseAudio-compatible socket; the system service lacks those session
environment variables. Direct ALSA hardware access does not require that socket.
Audio has been exercised from standalone CLI processes under `user`, not from
the live service. No group membership, audio-server configuration, or device
permissions were changed.

Inspect the current environment without Jarvis:

```bash
cat /proc/asound/cards
arecord -l
aplay -l
arecord -L
aplay -L
ls -l /dev/snd /dev/snd/by-id
id
lsusb
pactl info
wpctl status
```

`pactl`, `wpctl`, and `lsusb` are optional diagnostics. Their absence does not
affect Jarvis. To inspect USB native capabilities, substitute the current card
and device numbers from the listings; at this audit the command was:

```bash
cat /proc/asound/card2/stream0
```

## Backend and device selection

The backend wraps the existing `arecord` and `aplay` utilities with standard
library subprocess management. This keeps hardware details outside the generic
application and dispatcher, supports SSH diagnostics, and introduces no runtime
Python dependencies. Structured device selection and WAV operations are separate
from this concrete backend; no abstract backend hierarchy is needed yet.

```bash
cd /home/user/voice-assistant
source .venv/bin/activate
python -m jarvis audio devices
```

The listing separates capture and playback, identifies Jabra by discovered
names, displays optional native driver metadata, and marks the configured
selection. Detection does not prove that a device can be opened or produce sound.
Permission failures are reported separately from an empty hardware listing.

Selectors are resolved against a fresh listing for each operation:

| Selector | Meaning |
| --- | --- |
| `jabra` | Require exactly one discovered Jabra interface in the requested direction. |
| Listed USB by-id identity plus `:0` | Prefer the physical device identity, resolved to its current named ALSA endpoint. |
| Listed ALSA alias, such as `USB:0` | Use that current card name and device number. |

For this Pi the exact physical selector is
`usb-0b0e_Jabra_SPEAK_510_USB_08C8C2AAA158022000-00:0`.
An ALSA alias can change when several similar USB devices are attached, so use
the physical identity when available. Missing or ambiguous selections fail
clearly; Jarvis does not silently switch to another microphone. A disconnection
between discovery and opening is reported through the utility error or deadline.

`--device` overrides configuration for record/play. Loopback uses
`--input-device` and `--output-device`. Environment settings provide defaults:

```bash
export JARVIS_AUDIO_INPUT=jabra
export JARVIS_AUDIO_OUTPUT=jabra
```

The order is explicit CLI flag, environment variable, then `jabra`. Audio
settings are read only by audio commands. Invalid settings, missing utilities,
and detached devices do not prevent the generic core commands from starting.

## Recording and playback

```bash
mkdir -p recordings
python -m jarvis audio record --duration 5 --output recordings/check.wav
python -m jarvis audio play recordings/check.wav
```

Recording accepts integer durations of 1–30 seconds; the default is five. The
target is PCM WAV, S16_LE, mono, 16000 Hz. This matches the audited Jabra's native
capture format. Other selected capture hardware may negotiate conversion through
ALSA `plughw`, or fail if it cannot support this target. Successful output is
checked for the requested format and exact frame count before publication.

Use an existing writable directory and a new `.wav` filename. Jarvis does not
overwrite files or symlinks and provides no overwrite flag. It records into a
private temporary file beside the destination, then publishes the validated WAV
with an atomic, no-overwrite hard link. The directory's filesystem must support
hard links; an unsupported filesystem produces an error. Mode 0600 limits access
to the recording owner. The incomplete file is removed on failure, timeout, or
cancellation. `recordings/` is ignored by Git; recordings saved elsewhere remain
the operator's responsibility.

Playback accepts regular, complete WAV files containing uncompressed 16-bit PCM,
one or two channels, 8000–192000 Hz, and a duration greater than zero and at most
60 seconds. The Python 3.11 `wave` reader determines supported container details;
compressed WAV, floating-point audio, MP3, and other containers are not supported.
Validation reads bounded chunks and rejects incomplete data before playback.
The input file is left unchanged.

Choose explicit devices as needed:

```bash
python -m jarvis audio record --device USB:0 --duration 5 --output recordings/other.wav
python -m jarvis audio play --device USB:0 recordings/other.wav
```

Although the Jabra advertised 16 kHz playback, a synthetic two-second WAV through
plain `plughw` finished in approximately 0.69 seconds. This happened with and
without nonblocking-open mode. The same file through the installed ALSA
sample-rate converter at 48 kHz took 2.043 seconds. The underlying device/driver
cause has not been established.

Jabra playback therefore uses
`samplerate:SLAVE="plughw:CARD=USB,DEV=0",RATE=48000`, with the actual card name
resolved at runtime. ALSA resamples to 48 kHz and `plughw` adapts channels to the
audited stereo output. Jarvis does not relabel samples or alter WAV headers.
Other output devices use plain named `plughw`. This Jabra policy has been
physically checked on SPEAK 510 only; other Jabra models are unverified.

The converter is supplied by the installed `libasound2-plugins` package.
On another Debian installation, the potential package command is:

```bash
sudo apt-get install alsa-utils libasound2-plugins
```

Obtain approval before installing system packages. This command may download
packages from configured Debian mirrors. No installation was needed or performed
for this sprint. No global ALSA, PipeWire, or PulseAudio configuration was edited.

## Explicit loopback smoke test

When near the Pi, run:

```bash
python -m jarvis audio loopback --duration 5 --input-device jabra --output-device jabra
```

Both endpoints are resolved before recording. Speak a short phrase during the
five-second capture, then listen to its replay. Loopback runs capture and playback
serially in a private temporary directory and removes the WAV on completion or
failure. Success means the utilities completed and metadata was valid; the CLI
cannot certify that a person heard intelligible sound. This command is not
continuous monitoring and never runs during ordinary startup.

The physical transport test on this Pi completed successfully:

| Check | Observed result |
| --- | --- |
| Recording | 80000 frames, mono, two-byte samples, 16000 Hz: exactly five seconds |
| Owned native capture PCM | RUNNING, S16_LE, mono, 16000 Hz |
| Owned native playback PCM | RUNNING, S16_LE, stereo, 48000 Hz |
| Observed playback interval | 5.029 seconds |
| Total loopback time | 10.209 seconds |
| Exit / cleanup | Exit 0; no temporary artifacts; capture/playback PCM closed afterward |

The PCM observation matched the audio utility's PID to the CLI parent, rather
than attributing another audio client's activity to Jarvis. Diagnostic audio and
temporary recordings were removed. The user was away from the Pi, so human
confirmation of audible, intelligible replay remains pending. Do not treat this
transport result as full acoustic acceptance.

## Cancellation, cleanup, and failures

Discovery has a five-second deadline. Capture and playback have their requested
or validated duration plus five seconds. Utility invocation uses argument lists,
no shell, disabled stdin, a fixed C locale for parsing, and a private process
group. Utilities open devices nonblocking and report capture/playback overruns
as failures rather than claiming a complete sample.

SIGINT and SIGTERM use the core's scoped signal mechanism. The callback records
intent; the subprocess runner checks it at most every 0.1 seconds during I/O.
On interruption or timeout, it terminates the private group, escalates to SIGKILL
after a one-second grace period if needed, drains output, and reaps the child.
Cleanup waits are bounded. Direct `KeyboardInterrupt` also cleans up. Previous
signal handlers are restored after the command. Application lifecycle and logging
cleanup use the existing context manager.

| Exit code | Meaning |
| --- | --- |
| 0 | Completed audio operation; core `run` also returns 0 on normal signal shutdown. |
| 1 | Expected validation, configuration, device, utility, or I/O failure. |
| 2 | Invalid CLI arguments. |
| 130 | Audio cancelled by SIGINT / Ctrl+C. |
| 143 | Audio cancelled by SIGTERM. |

Cancellation is a stopped lifecycle, not an application failure. Actual errors
preserve the failure lifecycle and nonzero result. Logs contain device identities,
paths, duration, lifecycle, and utility diagnostics, never captured PCM samples.
Power loss and SIGKILL cannot run Python cleanup; an interrupted recording's
private temporary file might then need removal after confirming no capture is
running.

| Symptom | Check / response |
| --- | --- |
| Utility unavailable | Check `command -v arecord` and `command -v aplay`; request approval for missing packages. Core commands still work. |
| No matching Jabra | Inspect USB connection and `audio devices`; use an exact listed ID. No fallback capture occurs. |
| Device busy | Inspect `wpctl status` or existing clients. Jarvis reports the conflict and does not suspend PipeWire or stop other clients. |
| Device disconnects / utility fails | Read the error diagnostic, reconnect, list devices again, and explicitly retry. No automatic reconnect loop runs. |
| Permission denied | Inspect `id` and `/dev/snd` permissions. Do not run Jarvis as root or change groups automatically. |
| Missing `samplerate` PCM/plugin | Confirm `libasound2-plugins` is installed and `aplay -L` lists the converter. Request approval for any package/configuration change. |
| Output exists / directory unavailable | Choose a fresh WAV filename in an existing writable directory supporting hard links. |
| Unsupported or incomplete WAV | Supply bounded 16-bit PCM mono/stereo WAV; the original file is preserved on failure. |
| Operation stalls | Wait for the bounded deadline or press Ctrl+C. The owned utility is terminated and temporary audio cleaned up. |

## Verification scope and service compatibility

The automated unittest suite uses fakes, synthetic WAVs, and real child processes
for OS-signal/deadline tests. It needs no audio hardware and opens no microphone.
It covers discovery, selection, parser/environment behavior, validation, errors,
timeouts, cancellation, no-overwrite races, metadata, process reaping, private
temporary cleanup, and existing core/lifecycle behavior. Run the commands in
[README](../README.md#tests-and-checks) to reproduce it.

The separate physical tests verified the capture/playback transport described
above. Human listening, acoustic quality, actual unplug-during-I/O behavior,
simultaneous competing playback, other Jabra models, and audio I/O inside a live
systemd service remain unverified. Those need explicit supervised checks.

The already running, enabled `jarvis.service` was not stopped, restarted,
reinstalled, or modified during Sprint 2. It retained PID 7480 and zero restarts
at verification. Its loaded application remains v0.2.0; newly launched CLI
processes use v0.3.0. The service still runs the standby core and never records.
Any future live service update or restart requires explicit approval. See
[operations](operations.md) for read-only journal/status checks and deployment.
No reboot, power cycle, or service-failure recovery test was performed.

## Streaming and future adapters

The file backend deliberately retains bounded WAV operations. Sprint 3 adds a
separate immutable PCM-frame contract, raw pipe capture/playback, finite buffers,
session ownership, cancellation, and metrics, using the same device selectors
and Jabra conversion helper. It creates no temporary WAVs. See
[audio streaming](audio-streaming.md) for implemented commands and physical tests.

Future work can consume and produce those frames through `StreamSession` while
owning provider-specific networking, rate adaptation, and turn/interrupt policy.
Automatic reconnection and coordination with the user audio server remain
unimplemented. There is no recognition, wake word, AI integration, Bluetooth
handling, or network exposure. The live service remains untouched in Sprint 3;
its loaded code is v0.2.0 while new CLI processes use v0.4.0.

References: [ALSA utility options](https://github.com/alsa-project/alsa-utils/blob/v1.2.8/aplay/aplay.1),
[Python 3.11 WAV reader](https://docs.python.org/3.11/library/wave.html), and
[Python subprocess deadlines](https://docs.python.org/3.11/library/subprocess.html#subprocess.Popen.communicate).
