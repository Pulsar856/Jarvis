# Gemini Live — Jarvis v0.5.0

Sprint 4 provides an explicit, bounded conversational prototype through the
existing PCM engine. Core, WAV, streaming diagnostics, and systemd remain
independent of Gemini. Software tests pass offline. Real API setup, generated
audio, microphone transmission, and audible intelligible dialogue are separate
live acceptance requirements and remain unverified.

## Audited baseline and official sources

The Raspberry Pi 5 runs Debian 12 ARM64, Python 3.11.2, ALSA/PipeWire, and a
Jabra SPEAK 510 USB. Baseline main/origin/main was clean at
`30bee7577934d0a93c3bf2713f1494ecf521b05a` (v0.4.0), with 152 passing tests.
No existing provider existed. The adapter attaches to `StreamSession` instead
of replacing audio, lifecycle, dispatch, or signal handling.

Official documentation reviewed on **12 October 2026**:

- [Google SDK Live introduction](https://ai.google.dev/gemini-api/docs/live-api/get-started-sdk).
- [Live capabilities, PCM, activity and interruptions](https://ai.google.dev/gemini-api/docs/live-api/capabilities).
- [Session lifetime and resumption](https://ai.google.dev/gemini-api/docs/live-api/session-management).
- [Official Python SDK reference](https://googleapis.github.io/python-genai/).
- [Pinned SDK implementation](https://github.com/googleapis/python-genai/tree/v2.29.0).
- [Developer API pricing](https://ai.google.dev/gemini-api/docs/pricing).
- [Rate limits](https://ai.google.dev/gemini-api/docs/rate-limits).
- [Gemini API terms](https://ai.google.dev/gemini-api/terms).

Default model: **`gemini-3.8-live`**, used by Google's current Live guide.
Other documented candidates are `gemini-3.1-flash-live-preview` and
`gemini-2.5-flash-native-audio-preview-12-2025`. Pricing lists free-tier usage
for these candidates. This is documentation evidence, not proof that this
particular key/project can use them. Previews can change or disappear.

`ai models` shows dated offline candidates. `ai models --remote` makes one
authenticated request for at most 50 model entries and reports an advertised
`bidiGenerateContent` action where available. Listings are not guarantees of
Live compatibility, free quota, or billing eligibility. Only an approved setup
test can establish current access to the configured model. There is no automatic
model fallback.

## Optional SDK installation

```bash
cd /home/user/voice-assistant
source .venv/bin/activate
python -m pip install -e '.[gemini]'
python -m pip check
```

This installation is network dependent. `google-genai==2.29.0` is an optional
extra; it supports Python 3.10+, including this Pi's 3.11. The official SDK
handles authentication, protocol serialization, and WebSocket session ownership,
reusing installed `websockets==15.0.1`. Jarvis has one network implementation.
Its core required dependency list remains empty. SDK import/client creation
occur only on an explicit live command.

The audited virtual environment already has the SDK. Installation added 15
packages and upgraded none:

| Package | Installed version |
| --- | --- |
| google-genai | 2.29.0 |
| google-auth | 2.61.0 |
| httpx / httpcore | 0.28.1 / 1.0.9 |
| requests / urllib3 | 2.34.2 / 2.8.0 |
| certifi / charset-normalizer | 2026.7.22 / 3.5.2 |
| cryptography / cffi / pycparser | 50.0.2 / 2.1.1 / 3.11 |
| pyasn1 / pyasn1-modules | 0.6.4 / 0.4.2 |
| distro / tenacity | 1.9.0 / 9.1.4 |

Installed transitive versions describe this environment, not a complete lock
file. Removing the extra is unnecessary to run offline commands. On a machine
without it, only the actual SDK serialization test is skipped.

## Private credentials

Use your existing key. Do not paste it into chat, Git, a source file, a command
argument, or the service unit. The following Bash prompt reads it without echo
or a literal key in shell history:

```bash
cd /home/user/voice-assistant
source .venv/bin/activate
read -r -s -p 'Gemini API key: ' GEMINI_API_KEY
printf '\n'
export GEMINI_API_KEY
export GEMINI_MODEL=gemini-3.8-live
```

Alternatively, create a private key-only file outside the checkout:

```bash
install -d -m 700 /home/user/.config/jarvis
(
  umask 077
  set -o noclobber
  read -r -s -p 'Gemini API key: ' jarvis_private_key
  printf '\n'
  printf '%s\n' "$jarvis_private_key" > /home/user/.config/jarvis/gemini-key
  unset jarvis_private_key
)
chmod 600 /home/user/.config/jarvis/gemini-key
unset GEMINI_API_KEY
export GEMINI_API_KEY_FILE=/home/user/.config/jarvis/gemini-key
```

The creation command refuses to overwrite an existing file. For key rotation,
edit that existing private file locally and keep mode 0600. It contains the key
alone, without `KEY=`, quotes, or shell instructions. Do not print its contents.

`GEMINI_API_KEY` takes precedence over `GEMINI_API_KEY_FILE`. The file must be
regular, owned by the current user, have no group/other permissions, be at most
4096 bytes, and be outside the repository. Final-component symlinks and FIFOs
are rejected. No other credential files, applications, `.env`, or accounts are
searched. Credentials remain private process memory; environment access by the
same local user is still possible.

The SDK is explicitly configured for the Gemini Developer API at
`https://generativelanguage.googleapis.com` (TLS WebSocket equivalent for Live).
Vertex AI, enterprise selection, custom Google base-URL environment overrides,
and HTTP environment proxies are disabled for this adapter. The key is sent as
the SDK's API-key header, never as a Jarvis CLI argument or URL parameter.

No key is configured in the Codex execution environment used for this sprint.
The offline model command never reads the key file. Bad provider settings do
not block `status`, `demo`, `run`, or audio commands.

After interactive testing:

```bash
unset GEMINI_API_KEY GEMINI_API_KEY_FILE GEMINI_MODEL
```

This removes shell variables, not the optional private key file.

## Commands and supervised acceptance

```bash
python -m jarvis ai models
python -m jarvis ai --help
python -m jarvis ai talk --help
```

These commands are offline. Review project access, billing state, applicable
quotas, and privacy terms before approving any live test. Jarvis does not change
the Google project. The following commands each contact Google and must be
invoked explicitly:

1. **Connection only**:

   ```bash
   python -m jarvis ai connect-test --model gemini-3.8-live
   ```

   Opens one Live session, checks SDK setup confirmation, and closes. No capture,
   text request, or audio generation. Connection/setup timeout is ten seconds,
   followed by bounded cleanup. Success proves setup access at that time.

2. **Generated audio**, after connection succeeds and generation is approved:

   ```bash
   python -m jarvis ai speak-test --duration 15
   ```

   Sends the fixed prompt asking for “Jarvis is ready.” and permits one brief
   response, with no microphone. It validates incoming PCM and drains playback.
   Speech is generated, so exact wording/duration is not guaranteed. Default
   connected limit is 30 seconds; no completed audio by the deadline is an error.
   A person near the Pi must confirm audible, intelligible output.

3. **Microphone conversation**, when the user is near the Pi and approves upload:

   ```bash
   python -m jarvis ai talk --duration 30 --max-turns 2
   ```

   Listen for a response, then speak again after playback finishes. Capture opens
   only after API setup succeeds. Default limits are 60 connected seconds/three
   completed spoken responses; CLI hard limits are 120 seconds/five responses.
   The first limit reached ends the session. Ctrl+C stops; SIGTERM also cancels.

Optional device selection uses the existing stable selectors:

```bash
python -m jarvis ai talk --input-device jabra --output-device jabra --duration 30
```

`JARVIS_AUDIO_INPUT`/`JARVIS_AUDIO_OUTPUT` apply to voice commands.
`GEMINI_MODEL` selects a model, overridden by `--model`.
`JARVIS_GEMINI_MAX_SECONDS` sets the connected-time default, overridden by
`--duration`. There is no credential CLI flag.

The optional `ai models --remote` request also needs approval during this sprint.
Failure of a candidate ends the command; switching models is an operator choice.
No real Gemini command has been run as part of Sprint 4. All live-functional
acceptance requirements are pending; human listening was deferred because the
user was away from the Pi.

## Audio and component boundaries

```text
Jabra -> existing StreamSession capture (16 kHz mono S16_LE / 20 ms)
      -> one ordered send -> Gemini SDK Live WebSocket
Gemini (24 kHz mono S16_LE) -> validated chunks -> bounded response FIFO
      -> 20 ms PCMFrame -> existing StreamSession playback
      -> installed ALSA converter -> Jabra native 48 kHz stereo
```

`providers/gemini` owns credentials, model configuration, SDK sessions,
provider errors, message parsing, and provider chunk framing.
`conversation.py` coordinates the narrow concrete adapter and existing audio
API. Provider SDK objects never enter `audio/`; no generic provider hierarchy
or competing audio engine is introduced.

Input uses the existing 640-byte frames and MIME `audio/pcm;rate=16000`, with
increasing sequence across capture restarts. Output requires MIME
`audio/pcm;rate=24000` (optional `channels=1`), checks all message audio parts,
and frames the actual 24 kHz bytes into 960-byte frames. It retains less than one
partial frame, rejects an incomplete final sample, and pads a final aligned
fragment with less than 20 ms silence, counted in metrics. Incorrect formats
fail instead of being relabeled. Jabra resampling/channel adaptation occurs in
the existing ALSA path; no Python resampler or intermediate WAV is added.

The official SDK uses asyncio. Only explicit provider coordination uses an
event loop. Small, retained executor calls bridge to the existing synchronous
audio API. Per-session locks serialize start/read/write/drain/close, including
during cancellation. Audio's existing one-worker-per-direction design,
selectors, process groups, and cleanup are preserved.

## Turns, interruption, buffering, and shutdown

Server automatic activity detection is enabled; a start of activity can
interrupt a response. Default sequential mode closes capture on the first
incoming response audio, sends `audio_stream_end`, plays/drains the response,
and opens a new capture session in the same Gemini connection. It avoids feeding
speaker output back into the microphone but cannot provide acoustic barge-in
while capture is closed.

`--full-duplex` keeps capture open during playback. Interruption behavior is
tested with fakes, but actual speech/echo handling is unverified. There is no
acoustic echo canceller; Jabra's own behavior is not a software guarantee.
Use sequential mode for initial supervised acceptance.

The receiver continues across the SDK's per-turn receive iterators. A
`generation_complete` event is not device drain. `turn_complete`/interaction
IDLE ends the provider interaction; interaction IN_PROGRESS prevents premature
completion. Playback finish then drains the existing engine and waits for the
native backend before counting a completed spoken response or resuming capture.

A confirmed interruption increments the response epoch, discards queued and
partial old audio, requests abort of current playback immediately, closes/reaps
it, and admits only fresh-epoch output. Bytes already audible cannot be recalled;
native device/converter latency is outside this queue guarantee.

All handoffs have limits:

| Boundary | Default behavior |
| --- | --- |
| Capture FIFO | Ten 640-byte frames; drop oldest and count loss on network lag. |
| Network input | One in-flight frame, serial ordered sends, at least 20 ms between completed sends, five-second send timeout. |
| Provider message | At most 64 parts and 96000 audio bytes. |
| Response FIFO | At most 720000 audio bytes (15 seconds at 24 kHz) and 1024 packets. |
| Whole response | At most 15 audio seconds; exceeding the cap ends the session. |
| Output framer | Less than one 960-byte partial frame. |
| Playback FIFO | Ten 960-byte frames; retry the same rejected frame for at most five seconds. |
| Native drain | Ten-second wait, still cancellable by the overall session limit. |
| Resource churn | At most 32 audio-session acquisitions per conversation. |

Configuration objects allow a response cap up to 30 seconds; no CLI option
increases it. Python asks for minimal kernel pipes, rounded to 16384 bytes on
this Pi. SDK WebSocket buffers and native ALSA/converter buffers add bounded
transport buffering beyond application FIFOs.

SIGINT/SIGTERM callbacks only record intent. The provider guard polls it at
50 ms intervals during awaits, requests cooperative stop, cancels network tasks,
and waits for owned cleanup. Pending bridge calls are retained; every audio
resource gets a stop/close attempt, even if another fails. Workers join, owned
children are reaped, and network/SDK clients close. Previous signal handlers
and scoped SDK logging settings are restored. Normal cancellation does not mark
the application failed and returns 130/143; actual errors return 1.
Programmatic `StreamControl.request_stop()` is supported.

Connected duration starts after setup; connect/cleanup time is additional.
SDK close waits are three seconds per asynchronous stage and the outer guard
allows up to fifteen seconds for cooperative cleanup. These are software
deadlines, not guarantees against an unresponsive OS or faulty external backend.

## Errors, quotas, and privacy

Missing key/SDK, authentication rejection, model unavailability, permission or
billing restriction, quota exhaustion, network closure, timeout, invalid format,
and excessive backpressure have concise separate diagnostics. SDK exceptions
are classified internally and replaced with fixed messages; raw provider text
and credentials are never echoed. Scope suppresses SDK/network payload logging,
including debug headers. Jarvis logs lifecycle and metadata only.

There are no automatic retries or reconnections, including authentication
failures. Model fallback, session resumption, context compression, tools, video,
and transcription are not requested. Server GoAway closes this bounded session;
it never silently resumes. Jarvis's two-minute hard limit is below Google's
documented longer audio-session/connection windows.

Free-tier availability is finite and subject to the selected project's actual
rate limits; check Google AI Studio's current project limits. Jarvis's bounds
reduce exposure but do not impose a monetary budget. A key does not prove that
billing is disabled, and no command changes billing or project settings.
Quota errors stop the session. Do not enable billing just to work around an error
without a separate decision.

Google's data-use terms vary by service, billing state, and region. Unpaid
services can involve model-improvement use and human review; the current terms
apply Paid Services data handling to EEA/Switzerland/UK use, including unpaid
quota, with additional regional availability restrictions. Review the linked
terms for your account/location before sending personal speech. This is not a
promise that free-tier data is handled identically everywhere.

Jarvis stores no recordings, transcripts, session tokens, or personal response
text by default. Audio exists temporarily in bounded memory/pipes and is sent
to Google only by explicit talk. Console metrics contain counts/timings only.
Normal `run`/systemd never opens a Gemini connection or microphone.

## Verification, diagnostics, and remaining work

Run the checks in [README](../README.md). Offline tests use synthetic keys,
fake SDK/provider sessions, fake audio transports, and synthetic subprocess PCM.
They cover format/order, connection and partial startup, input/output backpressure,
multi-turn receive, interruption during write/startup, duration limits, cleanup,
safe diagnostics, and real SIGINT/SIGTERM during connection, capture, and playback.
The optional actual-SDK serialization test replaces the WebSocket and performs
no API request. Tests require no real key or hardware.

Final verification on the audited Pi: **203 tests passed** (the original 152
plus 51 Sprint 4 tests). `compileall`, `pip check`,
`git diff --check`, and `systemd-analyze verify deploy/systemd/jarvis.service`
also passed. Standalone CLI and editable package metadata report v0.5.0.
Real Gemini API requests performed: **zero**.

An explicit local-only 24 kHz silent Jabra test on 12 October 2026 produced
100 frames/96000 bytes, zero drops/discards/errors, native drain in 2.043 seconds,
first complete pipe write in 1.45 ms, and a 16384-byte pipe. It used existing
`audio stream-play-test --sample-rate 24000 --duration 2`, not Gemini.
The device's shared converter was already checked at native 48 kHz in Sprint 3.

Conversation metrics include input/drop counts, playback backpressure/discards,
buffer underflows/waits/high water, pipe capacity/waits, received/drained turns,
interruptions, queue/stale byte counts, final padding, first provider audio,
first playback pipe write, send waits, active time, and cleanup failures/time.
Empty-buffer underflows are expired application waits, not hardware underruns.
Pipe/provider timings do not measure audible latency. No real Gemini latency,
voice quality, long-run stability, quota behavior, or acoustic barge-in has been
measured.

Troubleshooting:

- Missing key: configure only the documented variable/private file locally.
- Missing SDK: install the optional extra in this virtual environment.
- Authentication: replace/revoke the key locally; do not copy the error/key into Git.
- Model/permission: check the configured identifier and current project access.
- Quota/billing: inspect current project limits; stop instead of retrying endlessly.
- Network/GoAway: inspect connectivity/provider status; begin a new explicit
  session only after resolving the cause.
- Audio device failure: use `audio devices` and existing local diagnostics.
  Do not reconfigure PipeWire, device permissions, or other services automatically.
- Format/protocol: confirm current model output against the pinned SDK/docs.
  Jarvis rejects unsupported audio rather than guessing.
- Backpressure: inspect capture loss, queue peaks, and playback waits; use short
  responses/sequential mode. Do not remove memory/session bounds.

The live service remains enabled/active with original PID 7480 and zero
restarts, running its already loaded v0.2.0. Sprint 4 makes no unit or operational
change. No API credential belongs in that unit. No reboot/power-cycle occurred.
See [operations](operations.md) for the earlier deployment and rollback procedure.
Manual Gemini commands stop independently; no live service rollout is needed.

Recommended next sprint: supervised real API and human listening acceptance,
then measured turn-taking/latency improvements using the same provider/audio
boundary. Additional providers can expose the same concrete operations/events
without SDK types in the engine. Wake word, persistence, autonomous capture,
device control, and service AI activation remain out of scope.
