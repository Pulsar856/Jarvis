# Jarvis architecture — Sprints 0–2

## Philosophy and implementation boundary

Jarvis is one Python process with explicit component boundaries. The smallest
useful core runs offline and has no runtime dependencies. The `src` package
layout and editable installation ensure imports work through the installed
package, rather than depending on the repository working directory.

The implemented system has CLI commands, validated configuration, logging,
lifecycle management, named synchronous dispatch, and a demonstration module.
Sprint 1 adds scoped Linux shutdown signals and a now-deployed systemd unit.
Sprint 2 adds optional device discovery and bounded WAV operations. Boot and
failure-recovery checks remain future operational work. Module registration is
an implemented extension convention. Streaming audio, AI providers, other device
adapters, persistence, and automation remain future concepts without placeholder
interfaces.

## Component responsibilities and dependency direction

| Component | Responsibility | Dependencies within Jarvis |
| --- | --- | --- |
| `__init__.py` | Canonical version constant | None |
| `__main__.py` | Exit using the CLI's result | `cli` |
| `cli.py` | Parse arguments, compose setup and scoped signals, format results and expected errors | `application`, `config`, `dispatch`, `logging`, `signals`, version |
| `config.py` | Validate immutable settings and documented environment overrides | Version |
| `logging.py` | Own handlers on the `jarvis` logger namespace | `config` |
| `application.py` | Own config, dispatcher, lifecycle state, and cooperative shutdown; consume recorded signals; register modules | `config`, `dispatch`, `modules.demo`, `signals` |
| `signals.py` | Temporarily own SIGINT/SIGTERM callbacks; record stop intent and restore previous handlers | None |
| `deploy/systemd/jarvis.service` | Define Linux process supervision, boot enablement, journal output, and restart limits | Installed CLI and interpreter paths |
| `dispatch.py` | Register handlers and return results with explicit errors | None |
| `modules/demo.py` | Implement and register the actual `hello` action | `dispatch` |
| `audio/devices.py` | Structured metadata, identities, and selection | `audio.errors` |
| `audio/backend.py` | ALSA subprocesses, deadlines, reaping, and Jabra playback conversion | Audio devices/errors, `signals` |
| `audio/operations.py` | WAV validation and file transactions; record/play/loopback | Audio backend/devices/errors, `signals` |
| `audio/cli.py` | Audio arguments, environment selectors, and terminal output | Audio backend/devices/operations/errors |

Modules should not import the CLI or application controller. The application
is the composition point that imports each concrete module's registrar.
Importing Jarvis creates no files, opens no hardware, and configures no logging.
Instantiation only prepares local state and the command table.

## Dispatch flow

For `python -m jarvis demo`:

```text
__main__.py -> cli.main()
  -> AppConfig.from_env()
  -> configure_logging(config)
  -> Application(config): register_demo(application.dispatcher)
  -> application.start()
  -> application.dispatch("hello")
  -> CommandDispatcher.dispatch("hello")
  -> modules.demo.hello() -> result
  -> CLI writes result to stdout
  -> application.shutdown()
  -> close_logging()
```

`CommandDispatcher.register(name, handler)` accepts a unique, nonempty name
without whitespace or control characters and a callable. Dotted names such as
`system.python` are supported. Duplicate registration raises `RegistrationError`
and preserves the original handler. `command_names` returns a sorted tuple.

A handler takes no arguments and returns an object. Dispatch returns that exact
object. There is currently no payload schema, context injection, queue,
subscription interface, timeout, or concurrent execution. Add a typed input
contract only when a concrete feature requires one.

An unknown name raises `UnknownCommandError`. Ordinary handler exceptions are
wrapped as `HandlerError` using `raise ... from exc`, preserving the original
exception in `__cause__`. The public error identifies the command and exception
type without repeating potentially sensitive cause values. `KeyboardInterrupt`
and other `BaseException` subclasses propagate. A caller may catch a dispatch
failure and continue using the application. A failure that escapes the lifecycle
context is logged as an application failure before cleanup, and propagates.

There is no interpretation of dispatched names as shell commands, no dynamic
import from user input, and no externally exposed dispatch API.

## Configuration and logging

`AppConfig` is a frozen dataclass. Direct construction and `from_env()` validate
settings. `from_env()` reads only `JARVIS_APP_NAME`, `JARVIS_LOG_LEVEL`, and
`JARVIS_LOG_DIR`; an explicit mapping can be supplied for isolated tests.
The version comes from `jarvis.__version__`, also used by setuptools package
metadata. No runtime version override or deployment mode is provided.

Settings are loaded once before application startup. There is no live reload
or configuration file precedence. Directory syntax is validated as configuration;
directory existence and writability are checked when file logging is configured.
Paths are portable and optional; device-specific paths are absent from the core.

`configure_logging()` replaces only handlers on the `jarvis` logger. Descendant
loggers, normally obtained with `logging.getLogger(__name__)`, inherit its level
and handlers. Propagation to the root logger is disabled to prevent duplicate
messages. Root and unrelated loggers are left alone. New handlers are prepared
before replacing the existing ones, so a failed file setup does not partially
replace a working configuration. `close_logging()` releases Jarvis handlers.

Console logs use stderr and contain a timestamp with local offset, severity,
logger name, and message. Optional file logs append the same format as UTF-8
to `jarvis.log`. File rotation is not implemented. `print()` is used for CLI
results and concise terminal diagnostics, not application lifecycle logging.

Future provider credentials belong at provider boundaries and must not appear
in configuration summaries, error strings, or logs. A private process environment
is enough for an initial integration; no vault, `.env` loader, or cloud setup is
present now. Ignoring credential files in Git is a convenience, not access control.

## Lifecycle

The controller exposes read-only state and these public operations:

| Operation | Contract |
| --- | --- |
| Construction | Prepare configuration, command registry, and shutdown signal in `initialized`. |
| `start()` | Allow `initialized -> running` once; reject repeat starts. |
| `dispatch(name)` | Require `running`; return the registered handler's result. |
| `run(signals=None)` | Require `running`; wait for cooperative stop, optional recorded SIGINT/SIGTERM, or a direct KeyboardInterrupt. |
| `request_shutdown()` | Set the cooperative shutdown signal and wake a waiting run loop. |
| `shutdown()` | Idempotently signal shutdown and transition to `stopped`. |
| Context manager | Start on entry and always shut down on exit; propagate exceptions. |

Normal lifecycle: `initialized -> running -> stopped`. An ordinary exception
leaving the context produces `running -> failed -> stopped`. The failed state
is recorded in logs before cleanup; the stopped state does not imply success.
The original exception still propagates. Handled SIGINT and SIGTERM request
shutdown and are not recorded as application failures. Repeated requests and
`shutdown()` calls produce one transition to `stopped`. A stopped application
cannot be restarted; construct another instance for a new lifecycle.

The run loop retains `threading.Event.wait(timeout=1.0)` as its blocking shutdown
primitive. It creates no threads or workers and sleeps between checks, with at
most one timeout wake per second. Recorded process signals are consumed in normal
loop execution, which logs the signal and calls the existing `request_shutdown()`
method. The finite wait bounds normal idle-loop signal response to approximately
one second. No interactive commands are processed while waiting. The lifecycle
context performs final cleanup after `run()` returns.

### Scoped Linux signals

The CLI's `run` and explicit `audio` commands install handlers. An `ExitStack` owns
`capture_shutdown_signals()` across configuration, startup, execution, application
shutdown, and logging cleanup. Its callbacks only record the first SIGINT/SIGTERM
in `ShutdownRequest`; repeated signals keep the same stop intent. They perform
no logging, locking, cleanup, or exception raising. In particular, invoking
`Event.set()` inside a signal callback could interrupt code already holding
Event's non-reentrant lock. Recording intent avoids that deadlock.

Python invokes signal callbacks on its main thread and permits handler
installation only there; see the [Python 3.11 signal documentation](https://docs.python.org/3.11/library/signal.html#signals-and-threads).
Installation errors are explicit CLI failures. The scope restores previous
handlers after all owned cleanup, even if execution fails. Partial installation
is rolled back. Foreign handlers reported by Python as `None` are rejected
before replacement because they cannot be restored through Python's API.

`Application.run()` alone installs no handlers, preserving embedding behavior
and the v0.1.0 API. It still catches a directly raised `KeyboardInterrupt`.
An embedding application may explicitly opt in using:

```python
from jarvis.application import Application
from jarvis.config import AppConfig
from jarvis.signals import capture_shutdown_signals

with capture_shutdown_signals() as signals:
    with Application(AppConfig.from_env()) as application:
        application.run(signals)
```

The caller retains ownership of its logging setup and teardown. Core `status`
and `demo` commands do not alter process handlers. SIGKILL and power loss cannot
run Python cleanup. Shutdown latency for future blocking handlers or resource
cleanup is not guaranteed by the idle-loop bound.

The CLI parses help/version before loading settings. One-shot `status` and `demo`
commands enter the same lifecycle context as `run`. `status` describes that
new application instance, not another process. Known failures produce exit 1,
usage errors exit 2, and normal core completion/SIGINT/SIGTERM exit 0. Audio
cancellation returns 128 plus the signal number (130 or 143) after cleanup. It is
caught inside the lifecycle context and does not mark the application failed.
Unexpected programming
errors retain their traceback after lifecycle and logging cleanup.

## Systemd service boundary

The system manager launches one foreground Python process as `user`, with the
repository working directory and existing virtual environment interpreter.
`Type=exec` detects interpreter execution failures. It does not implement an
application readiness notification or health watchdog. No shell activation,
daemon fork, PID file, graphical session, or network-online dependency is needed.

`WantedBy=multi-user.target` provides normal boot activation once explicitly
enabled. `Restart=on-failure` leaves successful exits and operator stops stopped.
Unexpected nonzero exits or process failures are retried after ten seconds.
Start limiting permits five starts within 300 seconds, including manual starts;
exhaustion leaves the unit failed until an operator fixes the cause and retries.
There is no Python process supervisor or automatic host reboot policy.

Systemd sends SIGTERM to the unit's control group and allows fifteen seconds for
shutdown; after that it can use SIGKILL. Both stdout and stderr go to the journal,
with unbuffered Python output. Optional application file logging remains available
for manual use. `NoNewPrivileges=true` prevents privilege gains, and `UMask=0077`
makes newly created service files private by default. There are no credentials
or extra capabilities in the unit.

The deployment paths are specific to this Pi and must be reviewed when moving
the service elsewhere; application logic remains path independent. The unit is
version controlled, syntax validated, and installed/enabled after Sprint 1
approval. Sprint 2 does not restart or alter it; the running service retains its
already loaded v0.2.0 code while new CLI processes use v0.3.0.
[Operations](operations.md) covers approval, deployment, updates,
logs, failure/reboot verification, and rollback. Existing SSH, Tailscale, audio,
Bluetooth, camera, and other services are not reconfigured.

Version 0.3.0 adds optional audio. Existing CLI, lifecycle, and module registration
contracts remain compatible with v0.2.0.

## Optional bounded audio

Audio is not registered as a background module. Explicit CLI commands call
`AudioService` within the existing application lifecycle. The generic dispatcher
keeps its no-argument contract; parameterized audio operations do not change it.
Neither imports nor construction enumerate or open devices. `run` creates no
audio service and starts no capture.

Flow: `audio CLI -> AudioService -> structured selection -> AlsaBackend ->
arecord/aplay argument list -> WAV result`. The backend owns subprocess and ALSA
details. Operations own WAV validation and file transactions. Replacing this
concrete backend does not require changes to the generic application; there is
no abstract backend hierarchy or automatic plugin discovery.

Discovery parses C-locale `arecord -l`/`aplay -l` results and optional USB driver
metadata. Linux by-id control links supply persistent USB identities, with named
ALSA aliases as fallback. Numeric card indices are current lookup metadata only.
The `jabra` selector requires exactly one match per direction and never falls
back to another microphone. Enumeration is not an access or health test.

Recording fixes the client format to mono S16_LE at 16000 Hz, verified native
on the audited Jabra. Durations are integer seconds in 1–30. The utility captures
an exact frame count. Complete WAV metadata and frames are checked before atomic,
non-overwriting hard-link publication from a private temporary file in the output
directory. Failure/cancellation removes temporary data. Output directories must
exist and support hard links; no overwrite flag is provided. Recordings use mode
0600. WAV validation reads bounded chunks, not an entire recording into memory.

Playback validates a regular, complete 16-bit PCM mono/stereo WAV with bounded
rate and duration, without altering the input. The audited Jabra returned too
quickly at 16 kHz despite advertised support. Jabra output therefore uses
`samplerate:SLAVE="plughw:CARD=...",RATE=48000`. The installed ALSA converter
preserves duration while adapting the WAV to 48 kHz native output; `plughw`
handles mono-to-stereo conversion. Headers remain unchanged and no global audio
configuration is edited. Other outputs use plain `plughw`. This policy has been
physically checked on SPEAK 510; other Jabra models remain unverified.
[Audio](audio.md) records timing evidence and dependencies.

Loopback resolves both endpoints before capture, records and plays serially in a
private temporary directory, and removes that directory on every exit path.
There is no simultaneous monitoring, continuous listening, or live streaming.

Child processes use fixed utility names resolved at invocation, argument
sequences, no shell, disabled stdin, and a private process group. Discovery has
a five-second deadline; I/O deadlines are duration plus five seconds. The runner
checks recorded signal intent every 0.1 seconds, requests SIGTERM on interruption,
escalates to SIGKILL after one second if necessary, and drains/reaps the child
with bounded waits. A directly raised KeyboardInterrupt also triggers cleanup.
Captured PCM goes only to private files, never application logs or CLI stdout.

`JARVIS_AUDIO_INPUT` and `JARVIS_AUDIO_OUTPUT` are read only by audio commands;
CLI flags take precedence. Their validation cannot block the generic core. No
profiles, servers, Python dependencies, or audio-session supervisors are added.

## Add a module in an independent development stream

1. Read this document, inspect current configuration and public lifecycle APIs,
   and run the baseline suite before changing code.
2. Add a focused module under `src/jarvis/modules/`. Keep import and registration
   free of I/O side effects. Implement a concrete callable and a typed
   `register(dispatcher)` function, following `demo.py`.
3. Choose a stable, unique command name. Add the registrar import and one explicit
   registration call in `Application.__init__`, alongside `register_demo()`.
4. Route named no-argument actions through `application.dispatch()`. Parameterized
   audio commands compose `AudioService` in the CLI without changing the dispatcher
   contract. Keep terminal formatting outside either kind of handler.
5. Add isolated tests for the result, failure behavior, and registration through
   the application. Use fakes for external services; make hardware tests opt-in.
6. Document settings, new dependencies, actual capabilities, and any changed
   contracts in this repository. Check compatibility with existing commands.

For example, a future local Python-version module could contain:

```python
import platform

from jarvis.dispatch import CommandDispatcher


def python_version() -> str:
    return platform.python_version()


def register(dispatcher: CommandDispatcher) -> None:
    dispatcher.register("system.python", python_version)
```

After explicitly importing and calling this registrar in the application, a
caller could use:

```python
from jarvis.application import Application
from jarvis.config import AppConfig

with Application(AppConfig.from_env()) as application:
    result = application.dispatch("system.python")
```

These snippets illustrate the convention; this example module is not installed
in Sprint 0. Do not replace existing demo registration or silently override names.

## Future integration strategy

Introduce additional audio, AI provider, camera, and connectivity adapters when there
is a selected, testable integration. Their I/O, credentials, and platform-specific
code should remain outside generic dispatch and configuration logic. Prefer
composing concrete Python modules over an automatic plugin framework.

When an adapter first owns a persistent resource, add the smallest explicit
startup/cleanup contract needed and test partial-startup failures. When streaming
or concurrent I/O becomes a requirement, evaluate asyncio and cancellation then.
The current synchronous handlers provide no timeout or task isolation, so do not
silently put an indefinitely blocking adapter behind them.

Reserve future namespaces through this documentation instead of empty packages:
providers, other devices, connectivity, and automation are possible boundaries,
not implemented APIs. The optional `audio` namespace is now implemented. Version
any public contract deliberately and update the
documentation when independent development streams integrate.

## Deliberately excluded technologies

Argparse, dataclasses, logging, and unittest meet the current needs. There is no
web framework, network server, distributed message bus, persistent queue,
database, workflow engine, async runtime, microservice, plugin discovery system,
or secret-management platform. None solves a concrete current requirement.
Previously installed third-party packages are not imported by Jarvis. Build-time
setuptools and wheel are declared separately from the empty runtime dependency
list. Linux process supervision is handled by systemd rather than a new Python
framework. The application itself changes no audio, Bluetooth, network, systemd,
or administrator configuration; installing the prepared unit is a separate,
approved operational action.

## Verification conventions

Install editable, then run `python -m unittest discover -s tests -v`.
Configuration tests use explicit mappings or isolated environments. Dispatch
tests check exact results, duplicates, unknown names, exception chaining, and
recovery. Lifecycle tests verify state guards, cleanup after failure, controlled
shutdown, and dispatch routing. CLI subprocesses have timeouts; the POSIX run
tests wait for readiness before sending real SIGINT or SIGTERM, then kill a
lingering child if necessary. Signal tests check repeated requests, previous
handler restoration, partial setup rollback, failure exit codes, and logging
cleanup. Logging tests use temporary directories and capture stderr. Unit syntax
is checked with a bounded `systemd-analyze verify` subprocess when supported,
without starting or installing the service. Audio tests use synthetic WAV files,
fake utilities, and mocks without opening microphones. Separate physical checks
validate PCM state, WAV metadata, playback timing, and temporary cleanup. The
user was away during testing, so audible confirmation remains pending.

Run `python -m compileall -q src tests` and `python -m pip check` for syntax
and environment consistency. CLI tests also verify installed package imports
outside the repository. Keep tests and the default application offline.
Live service stop/restart, failure recovery, reboot, and hardware power-restoration
tests are separate supervised operations and have not been claimed as automated
test results. Sprint 2 ends at bounded local audio. Streaming, providers,
recognition, and Bluetooth remain future work.
