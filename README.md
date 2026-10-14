# Project JARVIS

Jarvis is a modular personal assistant intended to run primarily on a Raspberry
Pi 5. The long-term vision includes voice conversation, interchangeable AI
providers, device control, and automation. **Sprints 0–4 implement an offline
core, Linux service operation, local WAV/PCM audio, and optional Gemini Live.**
Core commands need no hardware or API key. Explicit audio commands need ALSA and the
selected device; they never contact cloud services.

## Current status

Version 0.5.0 provides standard Python packaging, a command-line interface,
validated environment configuration, console and optional file logging, an
explicit application lifecycle, and synchronous command dispatch. The built-in
`hello` handler demonstrates the module convention. There are no runtime
dependencies beyond the Python standard library for core/audio. The optional
Gemini extra pins Google's official SDK. Sprint 1 adds graceful SIGINT
and SIGTERM shutdown and a systemd service. Sprint 2 adds device discovery,
bounded PCM WAV recording/playback, and a temporary-file loopback test. Sprint 3
adds incremental raw PCM capture/playback, bounded buffers, cancellation, session
ownership, and metrics.

Sprint 4 adds explicit, bounded Gemini connection, generated-audio, and voice
conversation commands through the existing PCM engine. Offline simulations pass;
real Gemini connectivity, microphone upload, and audible dialogue remain
unverified.

The approved service deployment was installed, enabled, and started on
12 October 2026. Sprints 2–4 found it running with its original PID and zero
restarts. It has not been stopped, restarted, or reconfigured for audio. Its
already imported code remains v0.2.0; newly launched CLI processes use v0.5.0.
Reboot, power-restoration, and service failure/recovery tests remain unperformed.

The minor increment to 0.5.0 reflects the new optional conversational provider.
Existing CLI commands and application/dispatch APIs remain compatible.

The primary environment is Raspberry Pi 5, Debian 12 Bookworm, ARM64/aarch64,
and Python 3.11.2. Python 3.11 or newer is required. Hardware access stays in the
optional backend. Real SIGINT/SIGTERM subprocess tests run on POSIX
systems. The unit is validated using the Pi's systemd 252 tools; no claim is
made that other platforms have been tested.

## Install

Use the existing virtual environment; do not install globally or recreate it:

```bash
cd /home/user/voice-assistant
source .venv/bin/activate
```

The existing Debian environment has setuptools and wheel available through
system Python. This editable install was verified without downloads:

```bash
PYTHONPATH=/usr/lib/python3/dist-packages \
  python -m pip install --no-index --no-deps --no-build-isolation -e .
python -m pip check
```

The temporary `PYTHONPATH` makes the locally installed build tools available to
this installation command. It is not needed or recommended when running Jarvis.
No existing packages are upgraded. This command assumes Debian's system build
tools are already present.

On another machine, activate a Python 3.11+ virtual environment and use:

```bash
python -m pip install --no-deps -e .
```

That standard command may access PyPI to obtain isolated build dependencies
(`setuptools` and `wheel`). It needs no credentials. Runtime execution and tests
remain offline. The default install adds no AI or hardware runtime packages.

For explicit Gemini operations, install the optional extra:

```bash
python -m pip install -e '.[gemini]'
python -m pip check
```

This command needs network access for `google-genai==2.29.0` and its dependencies.
It is already installed in the audited virtual environment. The SDK supports
Python 3.11 and reuses the existing WebSocket package; Jarvis adds no competing
network stack. No unrelated packages were upgraded. Core/audio commands and
offline tests work without the extra; only the actual SDK serialization test
skips when the extra is absent.

## Run

```bash
python -m jarvis
python -m jarvis --help
python -m jarvis --version
python -m jarvis status
python -m jarvis demo
python -m jarvis run
```

The installed `jarvis` command is equivalent to `python -m jarvis`.

- No arguments or `--help`: display identity, version, and available commands.
- `status`: start a short-lived application, display version, configuration,
  registered commands, and Python/platform details, then shut down. It does not
  query another running Jarvis process or probe external devices.
- `demo`: dispatch the registered `hello` command through the application and
  print `Hello. Jarvis core is operational.`, then shut down.
- `run`: start and wait for shutdown. **Press Ctrl+C in that terminal to stop
  cleanly.** SIGTERM, including `systemctl stop` after deployment, also requests
  graceful shutdown. The run loop consumes the request within its one-second
  wait interval, then lifecycle and logging cleanup complete. This is a standby
  process, with no voice input or interactive command prompt yet.

Normal completion, Ctrl+C, and handled SIGTERM return exit code 0. Known
configuration, filesystem, lifecycle, signal setup, and dispatch errors return 1
with a concise diagnostic.
Invalid CLI arguments return 2. CLI results go to stdout; application logs and
error messages go to stderr.

## Explicit local audio

```bash
python -m jarvis audio devices
mkdir -p recordings
python -m jarvis audio record --duration 5 --output recordings/test.wav
python -m jarvis audio play recordings/test.wav
python -m jarvis audio loopback --duration 5
```

`recordings/` is ignored by Git. Recording never overwrites existing output;
choose a fresh filename for another capture. Record/loopback accept integer
durations from 1 to 30 seconds. Playback accepts complete, uncompressed 16-bit
PCM mono/stereo WAV, 8000–192000 Hz, up to 60 seconds. Loopback resolves both
devices before capture, uses a private temporary directory, plays the result,
and removes the temporary audio on completion or failure.

The default selector is `jabra`; a missing or ambiguous Jabra is a clear error,
with no fallback to a different microphone. Linux USB by-id identities are
preferred; named ALSA aliases such as `USB:0` are also accepted. Choose an exact
ID from `audio devices` when multiple devices are attached. Numeric card indices
are current discovery metadata rather than persistent identities.

```bash
python -m jarvis audio record --device USB:0 --duration 5 --output recordings/other.wav
python -m jarvis audio play --device USB:0 recordings/other.wav
python -m jarvis audio loopback --input-device jabra --output-device jabra --duration 5
```

`JARVIS_AUDIO_INPUT` and `JARVIS_AUDIO_OUTPUT` optionally supply selectors;
explicit CLI flags take precedence. Only explicit audio/voice commands read
these settings. Invalid settings or missing utilities/devices do not prevent core commands from
starting. Enumeration reports detection and configuration, not functional health.

The observed Jabra SPEAK 510 captures native 16 kHz mono S16_LE. Jabra playback
uses the installed ALSA sample-rate converter at 48 kHz, with channel conversion
through `plughw`. This avoids premature playback completion observed at 16 kHz
on this device; saved WAV metadata remains accurate and unchanged. Other outputs
use their named `plughw` endpoint.

`arecord`/`aplay` come from `alsa-utils`; the Jabra converter comes from
`libasound2-plugins`. Both are already installed on the audited Pi. System package
installation and Linux audio configuration changes require approval. See
[the audio guide](docs/audio.md) for evidence, formats, commands, and limits.

Ctrl+C or SIGTERM during audio cancels and reaps the child utility and removes
incomplete temporary data. Audio cancellation returns 130 for SIGINT or 143 for
SIGTERM; core `run` shutdown remains exit 0. No audio starts automatically in
`run` or the systemd service.

## Explicit PCM streaming

```bash
python -m jarvis audio stream-info
python -m jarvis audio stream-test --duration 5
python -m jarvis audio stream-play-test --duration 2
```

`stream-info` opens no devices. `stream-test` captures fixed PCM frames and
reports metrics, with no speaker output or saved audio. `stream-play-test` sends
bounded silence through the playback transport. To speak and hear a replay when
near the Pi, explicitly enable sequential replay:

```bash
python -m jarvis audio stream-test --duration 5 --replay
```

Capture closes before replay begins; there is no acoustic feedback route. Replay
retains at most the requested duration in memory and creates no WAV files.
Durations are 1–30 seconds, rounded down to whole frames, with an overall deadline
of duration plus five seconds per stage. `run` never starts a stream.

The default logical format is S16_LE, 16000 Hz, mono, 20 ms: 320 samples and
640 bytes per frame. Optional flags configure `--sample-rate`, `--channels`,
`--frame-ms`, `--buffer-frames`, `--io-timeout`, and input/output selectors.
Defaults are ten queue slots and a two-second I/O timeout. Capture drops the
oldest queued frame on overflow and counts loss; playback waits or raises a
timeout without silently dropping admitted audio. SIGINT/SIGTERM return 130/143
after owned workers and child processes are released.

The physical Jabra produced all 250 frames of a five-second test with zero drops;
the first frame arrived in 36.5 ms. Two seconds of silent output drained in
2.044 seconds at native 48 kHz stereo. A separate two-second simultaneous capture
and silence-only playback test passed; captured audio was never routed to the
speaker. Those are transport observations, not acoustic latency or sound-quality
measurements. Human listening remains deferred. The Pi's minimum pipe capacity
is 16 KiB, so buffering limits still matter for future latency work.

See [the streaming guide](docs/audio-streaming.md) for the frame API, metrics,
ownership, actual format negotiation, test scope, and future adapter boundary.

## Explicit Gemini Live

```bash
python -m jarvis ai models
python -m jarvis ai --help
```

These commands are offline. The default model is configurable `gemini-3.8-live`,
matching the [current Google guide](https://ai.google.dev/gemini-api/docs/live-api/get-started-sdk)
reviewed on 12 October 2026. Google lists free-tier usage, but access, actual
quotas, billing, and regional terms depend on your project. A key alone does not
guarantee zero charges. Model listing is not proof of Live access.

Follow [the Gemini guide](docs/gemini-live.md) to configure a key privately
outside this repository and review cost/privacy terms. After explicit approval,
run each live stage separately:

```bash
python -m jarvis ai connect-test
python -m jarvis ai speak-test --duration 15
python -m jarvis ai talk --duration 30 --max-turns 2
```

These commands contact Google. `connect-test` confirms setup and closes without
microphone capture or generation. `speak-test` requests one brief spoken response.
`talk` opens the microphone only after successful setup; sequential mode closes
capture during speech playback and resumes after native drain. Ctrl+C/SIGTERM
cancel with exit 130/143. Default talk limits are 60 seconds and three spoken
turns, with hard maxima of 120 seconds/five turns. No automatic retries,
reconnections, paid-model fallback, saved recordings, or transcripts are added.
The `--full-duplex` flag is experimental; acoustic barge-in is unverified.
Provider audio is checked as 24 kHz mono S16_LE and played through existing
ALSA conversion to the Jabra's 48 kHz path.

Normal `run` and systemd operation stay offline and open no microphone.
No real Gemini API test was performed in Sprint 4; each needs user approval.

## Linux service operation

The [systemd unit](deploy/systemd/jarvis.service) uses the system manager
and runs the existing virtual environment's interpreter as the `user` account.
It is enabled through `multi-user.target`, works without a desktop or network
readiness, and sends stdout/stderr to the journal. Failure restarts wait ten
seconds and are limited to five starts in five minutes. Operator stops remain
stopped. SIGTERM is given fifteen seconds for cleanup before systemd may force
termination.

The unit is **installed, enabled, and running** on the audited Pi. Follow the
[operations guide](docs/operations.md) for deployment, journal commands,
restart/reboot verification, controlled failure testing, and rollback. Manual
CLI use continues to work independently of systemd. Being alive does not imply
that the service is listening or providing AI functionality. Do not restart or
modify the live service for Sprint 4. The operations
guide records the earlier deployment; supervision behavior is unchanged.

## Configuration

Configuration is loaded once per command and validated before startup.

| Setting | Default | Behavior |
| --- | --- | --- |
| `JARVIS_APP_NAME` | `Jarvis` | Display name; cannot be blank or contain control characters. |
| `JARVIS_LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR`, or `CRITICAL`; case insensitive in the environment. |
| `JARVIS_LOG_DIR` | Unset | Optional directory; created as needed, with UTF-8 output appended to `jarvis.log`. |
| Application version | `0.5.0` | Defined by `jarvis.__version__`; not overridden by the environment. |
| `JARVIS_AUDIO_INPUT` | `jabra` | Capture selector for explicit audio/voice commands; CLI flags override it. |
| `JARVIS_AUDIO_OUTPUT` | `jabra` | Playback selector for explicit audio/voice commands; CLI flags override it. |
| `GEMINI_API_KEY` | Unset | Private interactive credential, read only for explicit live AI commands. |
| `GEMINI_API_KEY_FILE` | Unset | Alternative private owner-held key file outside the repository. |
| `GEMINI_MODEL` | `gemini-3.8-live` | AI model; `--model` overrides it. |
| `JARVIS_GEMINI_MAX_SECONDS` | `60` | Connected-session cap, 1–120; `--duration` overrides it. |

```bash
JARVIS_APP_NAME="Jarvis Lab" JARVIS_LOG_LEVEL=DEBUG python -m jarvis status
JARVIS_LOG_DIR=./logs python -m jarvis demo
```

Log timestamps include the host's local UTC offset. Relative log directories
are resolved from the working directory; `~` is expanded. An explicitly empty
directory value or a path containing control characters is rejected. An
unavailable directory or an unwritable log file causes a clear startup error.
Omit `JARVIS_LOG_DIR` for console-only logging. File rotation is not implemented.

No development/production switch is provided because there is currently no
behavior that would need it. `.env` files are not loaded.
The existing `config/` skeleton remains reserved and unused.

### Provider credentials

Gemini credentials stay at the provider boundary, outside `AppConfig`, status
output, and systemd. The optional key file contains only the key, with mode 0600;
it is not an environment script. Never put a key in Git, CLI arguments, chat,
configuration summaries, or logs. See the [private setup procedure](docs/gemini-live.md#private-credentials).

## Tests and checks

After installing the editable package:

```bash
python -m unittest discover -s tests -v
python -m compileall -q src tests
python -m pip check
git diff --check
systemd-analyze verify deploy/systemd/jarvis.service
```

The standard-library suite covers configuration, lifecycle transitions, dispatch,
demo execution, CLI behavior, logging, error handling, scoped signal restoration,
repeated shutdown, graceful SIGINT/SIGTERM shutdown, and optional audio. Audio
tests use mocks, synthetic WAV files, and fake utilities; the automated suite
opens no microphone. Real child-process tests verify cancellation, deadlines,
and reaping. Streaming tests add frame contracts, backpressure, partial I/O,
startup rollback, worker joining, and real signals against synthetic utilities.
Physical Jabra checks are separate explicit operations.
Gemini tests use synthetic credentials, fake providers/audio, synthetic capture
processes, and real OS signals during connection, capture, and playback. The
optional real SDK test replaces its WebSocket with a fake and checks serialization
without contacting Google. No automated test makes a network request.
CLI subprocess tests have five-second deadlines and kill a lingering child on
failure. Unit syntax validation has a ten-second timeout and runs when the local
systemd utility and
target interpreter are available. None of these checks starts the service.
File tests use temporary directories. Tests require no keys or hardware and make
no network calls.

## Project structure

```text
voice-assistant/
├── pyproject.toml             # Package metadata and CLI installation
├── src/jarvis/
│   ├── __init__.py            # Package identity and version
│   ├── __main__.py            # python -m jarvis entry point
│   ├── cli.py                 # Argument parsing and terminal output
│   ├── config.py              # Immutable, validated settings
│   ├── logging.py             # Console/file handlers for the jarvis namespace
│   ├── signals.py             # Temporary SIGINT/SIGTERM handlers and stop intent
│   ├── application.py         # Lifecycle, shutdown, and central dispatch access
│   ├── dispatch.py            # Named handler table and explicit errors
│   ├── conversation.py        # Bounded provider/audio turn orchestration
│   ├── providers/gemini/      # Lazy SDK, settings, safe errors, events, framing, CLI
│   ├── audio/
│   │   ├── __init__.py        # Optional namespace; no hardware side effects
│   │   ├── devices.py         # Structured metadata and stable selection
│   │   ├── backend.py         # ALSA subprocesses, deadlines, and reaping
│   │   ├── operations.py      # WAV safety, recording, playback, loopback
│   │   ├── errors.py          # Expected failures and cancellation
│   │   ├── frames.py          # Immutable S16_LE PCM frame contract
│   │   ├── control.py         # Cooperative stop and bounded wait validation
│   │   ├── buffers.py         # Finite handoff and explicit loss accounting
│   │   ├── streaming.py       # Raw PCM pipe transport and process ownership
│   │   ├── session.py         # Subordinate workers, lifecycle, and metrics
│   │   ├── stream_cli.py      # Opt-in bounded streaming diagnostics
│   │   └── cli.py             # Audio arguments, settings, and output
│   └── modules/
│       ├── __init__.py
│       └── demo.py            # Concrete hello handler and registration
├── tests/test_*.py            # Offline unittest coverage
├── docs/architecture.md       # Boundaries and contributor integration guide
├── docs/operations.md         # Deployment, journal, recovery, and rollback
├── docs/audio.md              # Audio audit, commands, limits, and verification
├── docs/audio-streaming.md    # PCM API, buffering, metrics, and smoke tests
├── docs/gemini-live.md        # Explicit cloud commands, credentials, limits, evidence
├── deploy/systemd/jarvis.service # Linux service definition; not auto-installed
├── config/.gitkeep            # Original skeleton; no active config files
├── .gitignore
└── README.md
```

The original `src/.gitkeep` and `tests/.gitkeep` are also retained. Editable
installation creates ignored package metadata under `src/`.

## Scope and next milestones

Implemented functionality is the core, deterministic demo, explicit local audio,
and the optional bounded Gemini prototype. Physical capture/playback transport was verified on the Jabra, including
WAV metadata, native PCM state, playback timing, and temporary cleanup. Human
confirmation of audible playback remains unverified.
Handlers are explicit no-argument Python callables that return a result.
Dispatch is synchronous, so a slow handler would block its caller. There is no
automatic plugin discovery, background work, event queue, persistence, scheduling,
network server, local speech recognition, wake word, camera, or Bluetooth
integration. Service deployment is complete; restart recovery, reboot, and
hardware power restoration still need supervised tests. There is no readiness
protocol or watchdog: an active service describes a running process, not
functional health.
SIGKILL and power loss cannot invoke Python cleanup. File rotation remains
unimplemented; journal retention follows the host's existing journal settings.

Sprint 4 ends at the offline-verified conversational prototype. Real Live API
setup, generated speech, microphone transmission, and human listening still need
separately approved supervised checks. A two-second 24 kHz silent Jabra playback
test drained 100 frames in 2.043 seconds with no loss; this establishes local
transport compatibility, not Gemini performance or audible dialogue.
Devices that disconnect cause explicit
errors and cleanup; automatic hotplug recovery and audio-server coordination
are not implemented. The concrete Gemini adapter consumes/produces validated PCM
through `StreamSession`; SDK types stay outside the audio engine. Future providers
can use the same boundary without an abstract plugin framework. The recommended
next sprint is supervised live acceptance and measured latency/turn-taking
improvements. No wake word, memory, device automation, or service AI activation
is implemented.

See [the architecture and integration guide](docs/architecture.md) before adding
a module. That document records the current extension conventions independently
of any development conversation.
