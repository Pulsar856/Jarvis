# Jarvis operations — Sprint 1

## Deployment status and approval boundary

The repository contains a validated service definition and signal-handling tests.
The implementation session has not installed, enabled, or started the live unit,
performed a service failure/recovery test, rebooted the Pi, or changed its power
configuration. Those operations require the user's approval under Sprint 1
Phase D. The commands below prepare a concrete supervised deployment procedure.

The unit targets Debian 12, systemd 252, the existing `user` account, and
`/home/user/voice-assistant/.venv/bin/python`. It is a system service running as
an unprivileged account; installation and service management use `sudo`, while
Jarvis itself runs without root. No other Pi service is modified.

## Architecture and shutdown

`deploy/systemd/jarvis.service` launches `python -m jarvis run` in the repository
directory. It uses the existing interpreter directly, without an activation shell,
graphical session, network-online dependency, or Python supervisor.

The CLI temporarily handles SIGINT and SIGTERM across startup, execution, and
cleanup. Each signal callback records stop intent only. The idle run loop checks
that intent at most one second later, logs the signal, and calls cooperative
`request_shutdown()`. Lifecycle cleanup produces `running -> stopped` once,
then logging handlers close and previous process signal handlers are restored.
Repeated signals during the managed scope keep the same stop intent. Ordinary
application errors still produce `running -> failed -> stopped` and a nonzero
process exit. Standalone `Application.run()` installs no process handlers.

Ctrl+C and handled SIGTERM exit 0. Known startup/runtime errors exit 1; invalid
CLI arguments exit 2. SIGKILL and loss of power cannot invoke Python cleanup.

The service policy is:

| Setting | Meaning |
| --- | --- |
| `Type=exec` | Track the foreground Python process and detect execution failure; no readiness notification. |
| `User=user`, `Group=user` | Run with the existing account's ordinary permissions. |
| `WantedBy=multi-user.target` | Start during normal Linux boot after explicit enablement. |
| `Restart=on-failure` | Retry unexpected nonzero exits or process failures; leave normal exits and operator stops stopped. |
| `RestartSec=10s` | Wait ten seconds before a retry. |
| `StartLimitIntervalSec=300`, `StartLimitBurst=5` | Permit five starts within five minutes, including manual starts. |
| `KillSignal=SIGTERM`, `KillMode=control-group` | Request shutdown of the unit's processes. |
| `TimeoutStopSec=15s` | Allow fifteen seconds for shutdown before systemd can force termination. |
| `StandardOutput=journal`, `StandardError=journal` | Capture CLI output and application logs in the journal. |
| `PYTHONUNBUFFERED=1`, `JARVIS_LOG_LEVEL=INFO` | Emit output promptly and retain lifecycle messages. |
| `NoNewPrivileges=true`, `UMask=0077` | Prevent privilege gains and make new service-created files private. |

After the start limit is exhausted, systemd does not automatically resume retrying
just because the interval expires. Fix the cause, reset the failed state, and
start explicitly. There is no watchdog or automatic host reboot. A process that
hangs without exiting is not detected by this policy.

## Preflight and package update

These checks do not install or start the service:

```bash
cd /home/user/voice-assistant
source .venv/bin/activate
PYTHONPATH=/usr/lib/python3/dist-packages \
  python -m pip install --no-index --no-deps --no-build-isolation -e .
python -m jarvis --version
python -m jarvis status
python -m jarvis demo
python -m unittest discover -s tests -v
python -m compileall -q src tests
python -m pip check
git diff --check
systemd-analyze verify deploy/systemd/jarvis.service
```

The editable install uses existing Debian build tools without downloads or
package upgrades. The target version is 0.2.0. Review `git status` and any existing
service configuration before deployment. If a live unit already exists, inspect
`systemctl cat jarvis.service` and save its unit and any drop-ins before replacing
them. The implementation audit found no existing Jarvis unit.

## Install, enable, and start — after approval

```bash
cd /home/user/voice-assistant
sudo install -m 0644 deploy/systemd/jarvis.service /etc/systemd/system/jarvis.service
sudo systemctl daemon-reload
sudo systemctl enable jarvis.service
sudo systemctl start jarvis.service
```

Repeating installation copies the same reviewed definition; enable and start are
idempotent for an already enabled/running unit. Replacing a running unit does not
apply its new process settings until restart. Enablement persists across boot;
starting runs it now. Neither command changes SSH, Tailscale, Bluetooth, camera,
network, or desktop configuration. System services do not load the interactive
shell's `.bashrc`, virtual-environment activation, or `.env` files.

Verify the supervised deployment:

```bash
systemctl is-enabled jarvis.service
systemctl is-active jarvis.service
systemctl status --no-pager --full jarvis.service
systemctl show jarvis.service \
  --property=User,Group,MainPID,ExecStart,Restart,RestartUSec,TimeoutStopUSec,Result,ExecMainStatus,NRestarts
sudo journalctl -u jarvis.service -b -n 50 --no-pager
```

Expected results are `enabled`, `active`, the `user` account, a nonzero main PID,
the correct interpreter/command, and initialization plus `initialized -> running`
messages. `Type=exec` can report active before Jarvis has finished Python startup;
confirm the startup messages before subsequent signal tests. An active process
does not certify audio, AI, hardware, or application health.

## Logs and normal operations

Recent and live logs:

```bash
sudo journalctl -u jarvis.service -b -n 100 --no-pager
sudo journalctl -u jarvis.service -f
```

Ctrl+C exits the live journal viewer without stopping Jarvis. Journal retention
and availability of earlier boots depend on the existing host configuration;
this sprint does not change journald settings. `-b` selects the current boot.

Stop and inspect cleanup:

```bash
sudo systemctl stop jarvis.service
systemctl is-active jarvis.service
systemctl show jarvis.service --property=Result,ExecMainCode,ExecMainStatus
sudo journalctl -u jarvis.service -b -n 30 --no-pager
```

Expect `inactive` (the `is-active` command then returns nonzero), a successful
service result, exit status 0, and `Received SIGTERM` followed by one
`running -> stopped` message for that run. Wait beyond the ten-second restart
delay and confirm it remains inactive. An explicit stop is not retried.

Start or restart:

```bash
sudo systemctl start jarvis.service
sudo systemctl restart jarvis.service
```

After Python code updates, run the preflight checks and refresh editable metadata
if the version or package metadata changed, then restart. A running interpreter
retains imported code until restarted even with an editable install. Python-only
updates do not require daemon reload. After unit-file changes, repeat the reviewed
`install` command, run `daemon-reload`, then restart and inspect the journal.

Disable automatic startup and stop now:

```bash
sudo systemctl disable --now jarvis.service
systemctl is-enabled jarvis.service
systemctl is-active jarvis.service
```

Expect `disabled` and `inactive`; both status commands return nonzero in this
state. To restore deployment later, enable and start again after approval.

## Controlled failure/recovery test — after approval

Perform this only during a supervised test of a running, healthy core. Deliberate
SIGKILL bypasses graceful cleanup so that the restart policy can be observed;
use `systemctl stop` for ordinary operation. Send the signal to Jarvis's main
process through its unit, rather than killing unrelated Python processes.

```bash
systemctl is-active jarvis.service
systemctl show jarvis.service --property=MainPID,NRestarts
sudo systemctl kill --kill-who=main --signal=SIGKILL jarvis.service
sleep 15
systemctl is-active jarvis.service
systemctl show jarvis.service --property=MainPID,NRestarts,Result,ExecMainStatus
sudo journalctl -u jarvis.service -b -n 60 --no-pager
```

Record the PID and restart count before the kill. Expect a recorded SIGKILL
failure, a retry after approximately ten seconds, an incremented restart count,
a new running PID, and a fresh initialization/running message. Do one failure
test, then verify normal `stop` and `start` behavior. Stop and investigate if
recovery fails; do not deliberately exhaust the rate limit as part of routine
verification. These steps have not been executed during implementation.

## Troubleshooting

- `203/EXEC`: check the interpreter path and execute permissions. Do not use a
  different system Python accidentally.
- `200/CHDIR` or `217/USER`: check the repository directory and existing account.
- `No module named jarvis`: repeat the editable install in the documented virtual
  environment, then verify `python -m jarvis demo` as `user`.
- Configuration or log-path errors: inspect the journal and unit/drop-ins with
  `systemctl cat jarvis.service`. Fix settings or directory permissions for `user`.
  The prepared unit uses console/journal logging and requires no keys.
- `start-limit-hit`: first fix the logged root cause, then run:

  ```bash
  sudo systemctl reset-failed jarvis.service
  sudo systemctl start jarvis.service
  ```

- No output: check the unit name, current boot filter, journal permissions, and
  effective log level. Use `sudo journalctl` if access is denied.
- An active process with no voice response: this core intentionally provides no
  audio or AI functionality. `python -m jarvis status` describes a separate
  short-lived instance; use systemctl/journalctl to inspect the managed process.

## Reboot and power restoration — separate supervised tests

These are four different layers:

1. **Hardware power restoration:** the board receives power and actually begins
   booting. Its behavior depends on the hardware/power/firmware setup, outside
   Jarvis; enabling a service cannot turn on an unpowered board.
2. **Linux startup:** Debian boots and reaches its normal targets.
3. **Systemd launching Jarvis:** the enabled `multi-user.target` unit starts the
   interpreter. A graphical boot also reaches this target.
4. **Jarvis operation:** the process initializes and remains alive. Future audio
   or AI readiness will need additional feature-specific checks.

For an approved reboot test, first verify enabled/active state and current startup
logs, then request:

```bash
sudo reboot
```

The SSH session ends. After reconnecting through the existing access method:

```bash
uptime -s
systemctl is-enabled jarvis.service
systemctl is-active jarvis.service
systemctl status --no-pager --full jarvis.service
sudo journalctl -u jarvis.service -b --no-pager
```

Record the new boot, a live main PID, and Jarvis startup messages from this boot
without manually starting it. Do not claim boot activation based only on unit
syntax or `is-enabled` before reboot.

For a separate approved power-restoration test, have local physical access, verify
boot enablement, and run `sudo systemctl poweroff`. Confirm Linux has finished
shutdown before removing the supply. Restore power and confirm the board actually
begins booting; then reconnect and repeat the post-reboot checks above. If the
board does not boot, investigate its hardware/power/firmware behavior separately;
no such settings are changed by this sprint. This controlled test checks power
restoration after clean shutdown, not resilience to abrupt outages or filesystem
damage. Neither reboot nor power-restoration behavior has yet been validated.

## Roll back or remove the deployment

To stop service management while retaining Jarvis:

```bash
sudo systemctl disable --now jarvis.service
```

Manual use still works with `.venv/bin/python -m jarvis run`. To remove the unit
installed by this procedure without deleting source code, tests, logs, or `.venv`:

```bash
sudo systemctl disable --now jarvis.service
sudo rm -f /etc/systemd/system/jarvis.service
sudo systemctl daemon-reload
systemctl list-unit-files jarvis.service --no-pager
```

No unit file should remain from this procedure. If an earlier unit was backed up,
restore that reviewed definition instead of deleting it, reload, and enable/start
only if the previous operational state calls for that. Review separately created
drop-ins before removing them; this procedure creates none. No destructive Git
reset or repository deletion is part of rollback.
