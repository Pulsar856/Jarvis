# Project JARVIS

Jarvis is a modular personal assistant intended to run primarily on a Raspberry
Pi 5. The long-term vision includes voice conversation, interchangeable AI
providers, device control, and automation. **Sprints 0–2 implement an offline
core, Linux service operation, and optional local USB audio.** Core commands need
no hardware or API key. Explicit audio commands need local ALSA utilities and the
selected device; they never contact cloud services.

## Current status

Version 0.3.0 provides standard Python packaging, a command-line interface,
validated environment configuration, console and optional file logging, an
explicit application lifecycle, and synchronous command dispatch. The built-in
`hello` handler demonstrates the module convention. There are no runtime
dependencies beyond the Python standard library. Sprint 1 adds graceful SIGINT
and SIGTERM shutdown and a systemd service. Sprint 2 adds device discovery,
bounded PCM WAV recording/playback, and a temporary-file loopback test.

The approved service deployment was installed, enabled, and started on
12 October 2026. Sprint 2 found it still running with its original PID and zero
restarts. It has not been stopped, restarted, or reconfigured for audio. Its
already imported code remains v0.2.0; newly launched CLI processes use v0.3.0.
Reboot, power-restoration, and service failure/recovery tests remain unperformed.

The minor increment to 0.3.0 reflects the optional audio subsystem. Existing
CLI commands and application/dispatch APIs remain compatible.

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
remain offline, and no AI or hardware packages are installed by this project.

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
explicit CLI flags take precedence. Only audio commands read these settings.
Invalid settings or missing utilities/devices do not prevent core commands from
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
modify the live service for Sprint 2 without explicit approval.

## Configuration

Configuration is loaded once per command and validated before startup.

| Setting | Default | Behavior |
| --- | --- | --- |
| `JARVIS_APP_NAME` | `Jarvis` | Display name; cannot be blank or contain control characters. |
| `JARVIS_LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR`, or `CRITICAL`; case insensitive in the environment. |
| `JARVIS_LOG_DIR` | Unset | Optional directory; created as needed, with UTF-8 output appended to `jarvis.log`. |
| Application version | `0.3.0` | Defined by `jarvis.__version__`; not overridden by the environment. |
| `JARVIS_AUDIO_INPUT` | `jabra` | Capture selector for explicit audio commands; CLI flags override it. |
| `JARVIS_AUDIO_OUTPUT` | `jabra` | Playback selector for explicit audio commands; CLI flags override it. |

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
behavior that would need it. Configuration files and `.env` files are not loaded.
The existing `config/` skeleton remains reserved and unused.

### Future credentials

When an AI adapter is implemented, it should read only its documented credential
variables at the adapter boundary. Supply credentials through the process
environment or a private, user-owned environment file with restricted permissions.
Do not commit keys, pass them in command-line arguments, or include them in
configuration summaries, exceptions, or logs. `.env` and `.env.*` are ignored by
Git; any eventual `.env.example` must contain placeholders only. No credential
loading or secret-management infrastructure is implemented in these sprints.

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
and reaping. Physical Jabra loopback is a separate, explicit manual test.
CLI subprocess tests have five-second deadlines and kill a lingering child on
failure. Unit syntax
validation has a ten-second timeout and runs when the local systemd utility and
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
│   ├── audio/
│   │   ├── __init__.py        # Optional namespace; no hardware side effects
│   │   ├── devices.py         # Structured metadata and stable selection
│   │   ├── backend.py         # ALSA subprocesses, deadlines, and reaping
│   │   ├── operations.py      # WAV safety, recording, playback, loopback
│   │   ├── errors.py          # Expected failures and cancellation
│   │   └── cli.py             # Audio arguments, settings, and output
│   └── modules/
│       ├── __init__.py
│       └── demo.py            # Concrete hello handler and registration
├── tests/test_*.py            # Offline unittest coverage
├── docs/architecture.md       # Boundaries and contributor integration guide
├── docs/operations.md         # Deployment, journal, recovery, and rollback
├── docs/audio.md              # Audio audit, commands, limits, and verification
├── deploy/systemd/jarvis.service # Linux service definition; not auto-installed
├── config/.gitkeep            # Original skeleton; no active config files
├── .gitignore
└── README.md
```

The original `src/.gitkeep` and `tests/.gitkeep` are also retained. Editable
installation creates ignored package metadata under `src/`.

## Scope and next milestones

Implemented functionality is the core, deterministic demo, and explicit local
audio. Physical capture/playback transport was verified on the Jabra, including
WAV metadata, native PCM state, playback timing, and temporary cleanup. Human
confirmation of audible playback remains unverified.
Handlers are explicit no-argument Python callables that return a result.
Dispatch is synchronous, so a slow handler would block its caller. There is no
automatic plugin discovery, background work, event queue, persistence, scheduling,
network server, AI provider, speech recognition, wake word, camera, or Bluetooth
integration. Service deployment is complete; restart recovery, reboot, and
hardware power restoration still need supervised tests. There is no readiness
protocol or watchdog: an active service describes a running process, not
functional health.
SIGKILL and power loss cannot invoke Python cleanup. File rotation remains
unimplemented; journal retention follows the host's existing journal settings.

Sprint 2 ends at bounded local audio. A proposed next sprint is a small local
PCM streaming contract with cancellation, hotplug recovery, and coordination
with the existing audio server. Provider, recognition, and Bluetooth work remain
separate future topics and are not implemented here.

See [the architecture and integration guide](docs/architecture.md) before adding
a module. That document records the current extension conventions independently
of any development conversation.
