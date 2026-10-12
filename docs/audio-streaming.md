# PCM streaming — Jarvis v0.4.0

Sprint 3 adds local incremental audio alongside the existing WAV commands. It
works offline with the installed ALSA utilities and Python standard library.
Streaming is an explicitly owned resource, never an automatic listening daemon.
`status`, `demo`, `run`, and the live systemd service retain their behavior.

## Architecture and frame contract

```text
arecord stdout -> CaptureStream -> finite capture FIFO -> owning consumer
owning producer -> finite playback FIFO -> PlaybackStream -> aplay stdin
```

`StreamSession` owns the enabled transports, buffers, and workers. The consumer
or producer can be the CLI today and a conversational adapter later; it does not
need to know ALSA arguments. Existing discovery, persistent selectors, error
classes, signal requests, process-group signaling, and Jabra conversion policy
are reused. The existing file operations are not routed through streaming.

`PCMFormat` is frozen metadata describing interleaved signed 16-bit little-endian
PCM. It validates integer rates of 8000–192000 Hz, one/two channels, and integer
frame durations of 10–100 ms that contain an integral number of samples.
Supported logical metadata does not prove every device supports that format.

For the default 16000 Hz mono / 20 ms format:

| Quantity | Value |
| --- | --- |
| Samples per channel per frame | 320 |
| Bytes per interleaved sample frame | 2 |
| Bytes per PCMFrame | 640 |
| PCM bytes per second | 32000 |

`PCMFrame` contains a validated format, exact immutable payload, nonnegative
sequence, and optional finite monotonic timestamp. Capture sequences start at
zero and increase. Playback rejects duplicates or decreasing sequences; gaps
are allowed because capture can report dropped frames. `captured_at` records
Python frame assembly, not a hardware/acoustic timestamp. Frames contain no WAV
headers. Payloads are excluded from repr and are never printed or logged.

## Continuous transport and physical conversion

Each active direction has one continuous utility process. Capture uses raw PCM
on stdout; playback consumes raw PCM on stdin. There are no temporary files,
short-recording concatenation, codecs, or Python resamplers. Explicit arguments
declare logical format, named PCM, a period matching frame duration, and a
four-period ALSA buffer. For the default format, requests are 20 ms / 80 ms.
Those are negotiated requests rather than guaranteed hardware settings.

Nonblocking descriptors and selectors handle partial reads/writes without
blocking indefinitely. Capture assembles at most one partial frame and rejects
incomplete EOF. Playback preserves all bytes across partial writes. stderr is
drained independently with at most 4096 bytes retained, so diagnostics cannot
deadlock the PCM pipe or accumulate without limit. Exit, broken-pipe, timeout,
and device errors propagate clearly.

The audited Jabra SPEAK 510 captures native S16_LE / 16000 Hz / mono. Playback
receives logical S16_LE / 16000 Hz / mono and uses the shared
`samplerate:SLAVE="plughw:CARD=...",RATE=48000` path. Installed ALSA plugins
perform resampling; `plughw` negotiates stereo channels. The measured physical
output is S16_LE / 48000 Hz / stereo. Frames retain their logical metadata.
Other outputs use named `plughw`. The Sprint 2 direct-16-kHz timing discrepancy
and required installed packages are described in [audio operations](audio.md).

Direct capture and playback use separate USB interfaces. A short simultaneous
capture plus silence-only output test worked on this Pi. That does not establish
acoustic full-duplex readiness. Other clients can still own a device; a busy
device produces an error. Jarvis never suspends PipeWire or kills another client.

## Concurrency, buffers, and backpressure

One worker per enabled direction owns all of that transport's pipe I/O and
cleanup. A capture-only or playback-only session has one worker and one utility;
a session enabling both has two of each. The owner consumes capture and produces
playback through bounded FIFOs. No asyncio, broker, or external supervisor is
introduced. Selector/condition waits sleep between checks.

Capacity defaults to ten frames and is configurable from 1 to 256. At the default
format, a ten-slot application FIFO holds 6400 PCM bytes / 200 ms of audio.

| Direction | Full-buffer policy |
| --- | --- |
| Capture | Drop the oldest queued frame, keep the newest, and count the loss. Sequence gaps remain visible. |
| Playback | Wait within the admission timeout; if still full, raise AudioTimeout without accepting or silently dropping the new frame. |

The owner may retry a playback admission timeout with the same frame, or stop.
Successfully admitted frames are drained on finish. Cancellation/failure aborts
instead and explicitly counts queued discards. A closed buffer drains queued
data, then returns None; cancelled waits raise. Waiting producers and consumers
check stop intent at most every 50 ms and are notified by session stop.

Memory is finite at each handoff. Each transport also holds one in-flight frame
and a bounded diagnostic tail. Python requests 4096-byte kernel pipes at process
creation. The audited Pi has 16 KiB pages, so Linux rounds the request to an
actual 16384 bytes, reported in metrics. That is up to 512 ms of logical 16 kHz
mono PCM if completely full; it is not the measured latency. ALSA/converter/device
buffers add separate delay. Frame size alone therefore does not prove a 20 ms
end-to-end path. Pipe transport may need replacement or pacing in future latency
work if these limits do not satisfy a measured conversational requirement.

Normal stream-test discards each consumed payload after accounting. Explicit
sequential replay retains at most the requested sample duration in memory:
960000 PCM bytes at default format for 30 seconds, and at most 23040000 bytes at
the maximum validated rate/channels. It creates no recording file. Playback of
dropped capture frames cannot restore lost samples; metrics show that loss.

## Ownership, cancellation, and lifecycle

Session states are `initialized -> starting -> running -> stopping -> stopped`.
An error records failed before cleanup, preserves its exception, and remains
visible through `metrics.failed`. A stopped session cannot restart. Construct
a new session for another operation. Resolve both selected endpoints before
opening either; partial process or worker startup rolls back acquired resources.

The CLI supplies its existing `ShutdownRequest`. Signal callbacks still only
record intent. Workers consume that intent during normal waits. Programmatic
`request_stop()` sets an Event and closes/discards buffers; it is safe from
another ordinary thread. SIGINT/SIGTERM cancellation returns 130/143 after
cleanup without marking the core application failed. Programmatic cancellation
raises StreamCancelled at blocked operations. Ordinary failures retain exit 1;
argument-parser failures use exit 2. Core `run` signal shutdown remains exit 0.

The owner calls `close()` or uses the context manager to join workers and reap
children. Stop requests and close are idempotent. Child cleanup sends SIGTERM to
the private process group, waits up to one second, then sends SIGKILL if needed
and waits up to one more second. Remaining descendants in that owned group are
also killed. Pipes/selectors close deterministically. Worker joining has a
shared three-second budget. Partial startup may additionally close transports
that never acquired a worker; each such close has the child deadlines above.
No shutdown path intentionally waits forever. SIGKILL or power loss cannot run
Python cleanup and have not been used as hardware recovery tests.

Device discovery retains its five-second deadline and existing runner. An
optional cancellation callback extends that runner for session owners, so
programmatic stop and signals also interrupt discovery, checked every 100 ms.
File-based callers keep their original contract and behavior.

`finish_playback(timeout=...)` is distinct from abort: it stops admission, drains
the FIFO, closes stdin, and waits for successful backend completion. Cancel or
close interrupts queued output instead. An idle playback worker checks unexpected
backend exit on its bounded queue waits, at most 250 ms apart. Capture failures,
including EOF/disconnection, cancel the other owned direction and wake the owner.
There is no automatic device reconnection or session restart policy.

## CLI commands and configuration

```bash
cd /home/user/voice-assistant
source .venv/bin/activate
python -m jarvis audio stream-info
python -m jarvis audio stream-test --duration 5
python -m jarvis audio stream-play-test --duration 2
```

`stream-info` reports logical PCM sizing, selectors, installed utility availability,
buffer policies, and limits without opening devices. `stream-test` captures and
reports metrics without speaker output or saved PCM. `stream-play-test` explicitly
sends silence to the selected speaker interface without capture.

When present at the Pi, perform the deferred listening check:

```bash
python -m jarvis audio stream-test --duration 5 --replay
```

Speak during capture and confirm intelligible replay at a comfortable volume.
Capture closes before playback starts, preventing a feedback route. This mode
was tested against synthetic utilities, not with unattended captured-voice output.
No command starts a permanent stream or modifies the managed service.

| Option | Default / rule |
| --- | --- |
| `--duration` | 5 seconds; integer 1–30, tests only. |
| `--sample-rate`, `--channels`, `--frame-ms` | 16000, 1, 20; PCMFormat validation applies. |
| `--buffer-frames` | 10; integer 1–256. |
| `--io-timeout` | 2 seconds; finite positive value up to 60. |
| `--input-device`, `--output-device` | Explicit flags override JARVIS_AUDIO_INPUT/OUTPUT, then jabra. |
| `--replay` | Off; stream-test only, sequential captured-audio output. |

Duration is rounded down to complete frames. A separate overall deadline of
duration plus five seconds per stage prevents a slowly progressing source/sink
from extending the CLI test indefinitely; cleanup deadlines follow. A configured
per-I/O timeout cannot extend the remaining stage budget. Library sessions may
remain active until their owner stops them, but every pipe/admission wait has a
deadline and buffering remains finite. Audio settings never block core commands.

## Metrics and verified results

CLI output includes JSON-compatible snapshots, without payloads:

| Metric | Interpretation |
| --- | --- |
| Transport frames/bytes | Complete frames assembled or completely written to the pipe. |
| Buffer accepted/consumed/dropped/discarded/occupancy | Accounting: accepted = consumed + dropped + discarded + occupancy. |
| high_water | Maximum application FIFO occupancy. |
| overflows | Full-buffer encounters, including successful playback waits; not necessarily loss. |
| underflows | Expired empty-buffer waits; not hardware underrun counters. Idle playback waits can increment them. |
| wait_seconds | Time in condition waits; transport io_wait_seconds measures selector waits. |
| process_start_seconds | Popen and pipe setup, excluding device discovery and CLI startup. |
| first_frame_seconds | Start-to-first complete capture frame or complete playback pipe write. |
| active_seconds | Transport wall time before close/drain, including startup. Logical audio duration is frames times frame duration. |
| cleanup_seconds | Per-transport close or owner join/close duration; a completed worker may have cleaned up before owner close. |
| pipe_capacity_bytes | Actual negotiated kernel PCM pipe capacity. |
| errors / failed | Transport operation failures and persistent session failure status. |

These are host/process/pipe observations, not microphone-to-speaker acoustic
latency. Native buffering, converter delay, scheduling, and device electronics
require separate instrumentation.

Physical checks on 12 October 2026 used explicit standalone commands as user:

| Test | Observed result |
| --- | --- |
| Five-second raw capture | 250 frames / 160000 bytes; all consumed, zero drops; first complete frame 36.5 ms after transport start. |
| Native capture | S16_LE, mono, 16000 Hz; period 320 samples, buffer 1280 samples. |
| Two-second silent raw playback | 100 frames / 64000 logical bytes, zero discards/loss; active/drain time 2.044 seconds; first pipe write 1.68 ms. |
| Native playback | S16_LE, stereo, 48000 Hz; period 960 samples, negotiated buffer 3843 samples. |
| Capture SIGINT / SIGTERM | Exit 130 / 143; approximately 61 ms from sent signal to observed CLI exit in each run; no failed lifecycle. |
| Simultaneous capture and silence-only playback | 100 frames each, zero drops, both owned native PCMs RUNNING; total 2.141 seconds. No captured audio sent to speaker. |
| Cleanup | Owned children reaped and workers joined; both native PCM interfaces closed after tests; no audio files created. |

PCM observations verified utility owner PIDs against the CLI/session, avoiding
attribution of another client's activity to Jarvis. Capture-only and silent
playback CLI wall times, including startup/observation overhead, were 5.158 and
2.170 seconds. These are single observations, not a latency benchmark or bound.

The automated suite remains hardware-free and offline. New tests cover validated
frames, sustained simulated production, slow consumers, finite FIFO capacity,
loss accounting, partial reads/writes, EOF, device failures, diagnostic floods,
timeouts, signal/programmatic cancellation, ignored SIGTERM escalation, startup
rollback, worker joining, ordering, and compatibility with existing WAV/core
commands. Synthetic real pipe processes and fake executable tools are distinguished
from the physical tests above. Run the full checks in [README](../README.md).

Human sound-quality confirmation, captured-voice streaming replay, actual unplug
during I/O, competing-client device-busy behavior, long-duration stability,
other devices/formats, and audio inside the live service remain unverified.
Automatic hotplug recovery, acoustic echo cancellation, speech recognition,
wake word, synthesis, and provider networking are not implemented.

The live jarvis.service remains enabled/active with PID 7480, zero restarts, and
its loaded v0.2.0 code. New standalone CLI processes use v0.4.0. No unit, system
package, device permission, PipeWire/PulseAudio/ALSA configuration, reboot, or
other service was changed. The earlier [operations guide](operations.md) remains
the deployment reference; any live restart/update needs explicit approval.

## Small extension boundary for a future conversational sprint

Use `StreamSession` inside the existing Application resource scope. One owning
consumer calls `read_frame(timeout=...)`; one owning producer calls
`write_frame(frame, timeout=...)`. Call `finish_playback()` to drain a completed
turn, or `request_stop()` followed by owner `close()` to abort the session. New
sessions replace stopped ones. An embedding application supplies its own signal
policy; sessions never install handlers themselves.

A local capture example, which explicitly opens the microphone:

```python
from jarvis.audio.session import StreamSession

session = StreamSession(capture_selector="jabra")
try:
    session.start(capture_frames=250)
    with session:
        while (frame := session.read_frame(timeout=2)) is not None:
            print(frame.sequence)  # Metadata only; an adapter can consume bytes.
finally:
    session.close()
```

A future adapter owns network framing, credentials, remote deadlines, rate
conversion, conversational turns, and cancellation policy. It should consume
logical PCM metadata and payloads, and assemble returned data into validated,
ordered PCMFrame objects. Different rate/frame-size contracts require explicit
adaptation; copying metadata does not resample audio. It must handle reported
capture gaps, playback backpressure, remote loss, and session failure without
unbounded queues. No provider interface, WebSocket, key loading, VAD, or AI
integration is included in Sprint 3.

References: [ALSA raw/stdio and buffering options](https://github.com/alsa-project/alsa-utils/blob/v1.2.8/aplay/aplay.1),
[Python selectors](https://docs.python.org/3.11/library/selectors.html), and
[Python subprocess pipe sizing](https://docs.python.org/3.11/library/subprocess.html#subprocess.Popen).
