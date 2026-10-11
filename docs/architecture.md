# Jarvis architecture — Sprint 0

## Philosophy and implementation boundary

Jarvis is one Python process with explicit component boundaries. The smallest
useful core runs offline and has no runtime dependencies. The `src` package
layout and editable installation ensure imports work through the installed
package, rather than depending on the repository working directory.

The implemented system has CLI commands, validated configuration, logging,
lifecycle management, named synchronous dispatch, and a demonstration module.
The module registration function is an implemented extension convention. Audio,
AI providers, devices, persistence, automation, and service operation are future
concepts; there are no placeholder adapters or interfaces for them.

## Component responsibilities and dependency direction

| Component | Responsibility | Dependencies within Jarvis |
| --- | --- | --- |
| `__init__.py` | Canonical version constant | None |
| `__main__.py` | Exit using the CLI's result | `cli` |
| `cli.py` | Parse arguments, compose setup, format results and expected errors | `application`, `config`, `dispatch`, `logging`, version |
| `config.py` | Validate immutable settings and documented environment overrides | Version |
| `logging.py` | Own handlers on the `jarvis` logger namespace | `config` |
| `application.py` | Own config, dispatcher, lifecycle state, and shutdown signal; register concrete modules | `config`, `dispatch`, `modules.demo` |
| `dispatch.py` | Register handlers and return results with explicit errors | None |
| `modules/demo.py` | Implement and register the actual `hello` action | `dispatch` |

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
| `run()` | Require `running`; wait until shutdown is requested or Ctrl+C occurs. |
| `request_shutdown()` | Set the cooperative shutdown signal and wake a waiting run loop. |
| `shutdown()` | Idempotently signal shutdown and transition to `stopped`. |
| Context manager | Start on entry and always shut down on exit; propagate exceptions. |

Normal lifecycle: `initialized -> running -> stopped`. An ordinary exception
leaving the context produces `running -> failed -> stopped`. The failed state
is recorded in logs before cleanup; the stopped state does not imply success.
The original exception still propagates. Ctrl+C requests shutdown and is not
recorded as an application failure. A stopped application cannot be restarted;
construct another instance for a new lifecycle.

The run loop uses `threading.Event.wait(timeout=1.0)` as a blocking shutdown
primitive. It creates no threads or workers and sleeps between checks, with at
most one timeout wake per second. The finite wait also keeps signal handling
responsive on platforms whose waits are less interruptible. This is not an
event processing loop. It cannot receive interactive CLI commands while waiting.
The CLI lifecycle context performs final cleanup after `run()` returns.

The CLI parses help/version before loading settings. One-shot `status` and `demo`
commands enter the same lifecycle context as `run`. `status` describes that
new application instance, not another process. Known failures produce exit 1,
usage errors exit 2, and normal completion or Ctrl+C exit 0. Unexpected programming
errors retain their traceback after lifecycle and logging cleanup.

## Add a module in an independent development stream

1. Read this document, inspect current configuration and public lifecycle APIs,
   and run the baseline suite before changing code.
2. Add a focused module under `src/jarvis/modules/`. Keep import and registration
   free of I/O side effects. Implement a concrete callable and a typed
   `register(dispatcher)` function, following `demo.py`.
3. Choose a stable, unique command name. Add the registrar import and one explicit
   registration call in `Application.__init__`, alongside `register_demo()`.
4. Route execution through `application.dispatch()`. Add CLI surface only if
   users need a new terminal operation; keep terminal formatting out of handlers.
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

Introduce audio, AI provider, camera, and connectivity adapters only when there
is a selected, testable integration. Their I/O, credentials, and platform-specific
code should remain outside generic dispatch and configuration logic. Prefer
composing concrete Python modules over an automatic plugin framework.

When an adapter first owns a persistent resource, add the smallest explicit
startup/cleanup contract needed and test partial-startup failures. When streaming
or concurrent I/O becomes a requirement, evaluate asyncio and cancellation then.
The current synchronous handlers provide no timeout or task isolation, so do not
silently put an indefinitely blocking adapter behind them.

Reserve future namespaces through this documentation instead of empty packages:
audio, providers, devices, connectivity, and automation are possible boundaries,
not implemented APIs. Version any public contract deliberately and update the
documentation when independent development streams integrate.

## Deliberately excluded technologies

Argparse, dataclasses, logging, and unittest meet the current needs. There is no
web framework, network server, distributed message bus, persistent queue,
database, workflow engine, async runtime, microservice, plugin discovery system,
or secret-management platform. None solves a concrete Sprint 0 requirement.
Previously installed third-party packages are not imported by Jarvis. Build-time
setuptools and wheel are declared separately from the empty runtime dependency
list. No audio, Bluetooth, network, systemd, or administrator configuration is
changed by the application.

## Verification conventions

Install editable, then run `python -m unittest discover -s tests -v`.
Configuration tests use explicit mappings or isolated environments. Dispatch
tests check exact results, duplicates, unknown names, exception chaining, and
recovery. Lifecycle tests verify state guards, cleanup after failure, controlled
shutdown, and dispatch routing. CLI subprocesses have timeouts; the POSIX run
test waits for readiness before sending SIGINT, then kills a lingering child
if necessary. Logging tests use temporary directories and capture stderr.

Run `python -m compileall -q src tests` and `python -m pip check` for syntax
and environment consistency. CLI tests also verify installed package imports
outside the repository. Keep tests and the default application offline.
The planned next step is optional audio discovery and bounded record/playback,
with explicit resource cleanup; cloud conversation follows only after that local
path is validated.
