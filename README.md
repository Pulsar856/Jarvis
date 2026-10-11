# Project JARVIS

Jarvis is a modular personal assistant intended to run primarily on a Raspberry
Pi 5. The long-term vision includes voice conversation, interchangeable AI
providers, device control, and automation. **Sprint 0 implements an offline
Python core only.** No API key, microphone, speaker, camera, or network connection
is needed to use it.

## Current status

Version 0.1.0 provides standard Python packaging, a command-line interface,
validated environment configuration, console and optional file logging, an
explicit application lifecycle, and synchronous command dispatch. The built-in
`hello` handler demonstrates the module convention. There are no runtime
dependencies beyond the Python standard library.

The primary environment is Raspberry Pi 5, Debian 12 Bookworm, ARM64/aarch64,
and Python 3.11.2. Python 3.11 or newer is required. The core avoids hardware
and operating-system-specific APIs. The real Ctrl+C subprocess test runs on
POSIX systems; no claim is made that other platforms have been tested.

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
  cleanly.** This is a standby process, with no voice input or interactive command
  prompt yet.

Normal completion and Ctrl+C return exit code 0. Known configuration,
filesystem, lifecycle, and dispatch errors return 1 with a concise diagnostic.
Invalid CLI arguments return 2. CLI results go to stdout; application logs and
error messages go to stderr.

## Configuration

Configuration is loaded once per command and validated before startup.

| Setting | Default | Behavior |
| --- | --- | --- |
| `JARVIS_APP_NAME` | `Jarvis` | Display name; cannot be blank or contain control characters. |
| `JARVIS_LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR`, or `CRITICAL`; case insensitive in the environment. |
| `JARVIS_LOG_DIR` | Unset | Optional directory; created as needed, with UTF-8 output appended to `jarvis.log`. |
| Application version | `0.1.0` | Defined by `jarvis.__version__`; not overridden by the environment. |

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
loading or secret-management infrastructure is implemented in Sprint 0.

## Tests and checks

After installing the editable package:

```bash
python -m unittest discover -s tests -v
python -m compileall -q src tests
python -m pip check
```

The standard-library suite covers configuration, lifecycle transitions, dispatch,
demo execution, CLI behavior, logging, error handling, and graceful run shutdown.
Subprocess tests have five-second deadlines and kill a lingering child on failure.
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
│   ├── application.py         # Lifecycle, shutdown, and central dispatch access
│   ├── dispatch.py            # Named handler table and explicit errors
│   └── modules/
│       ├── __init__.py
│       └── demo.py            # Concrete hello handler and registration
├── tests/test_*.py            # Offline unittest coverage
├── docs/architecture.md       # Boundaries and contributor integration guide
├── config/.gitkeep            # Original skeleton; no active config files
├── .gitignore
└── README.md
```

The original `src/.gitkeep` and `tests/.gitkeep` are also retained. Editable
installation creates ignored package metadata under `src/`.

## Scope and next milestones

Implemented functionality is limited to the core and deterministic demo.
Handlers are explicit no-argument Python callables that return a result.
Dispatch is synchronous, so a slow handler would block its caller. There is no
automatic plugin discovery, background work, event queue, persistence, scheduling,
network server, audio, AI provider, camera, Bluetooth, or systemd integration.
SIGTERM handling and service operation are future work; Ctrl+C is supported now.

A proposed Sprint 1 is a small, optional local audio adapter: first enumerate
input/output devices, then implement explicit resource opening/closing and a
bounded record/playback smoke test. Keep core tests hardware-free and make
hardware verification opt-in. Validate the selected microphone and speaker path
before adding real-time AI providers or wake-word detection in later sprints.
This proposal is not implemented here.

See [the architecture and integration guide](docs/architecture.md) before adding
a module. That document records the current extension conventions independently
of any development conversation.
