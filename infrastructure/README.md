# Deployment helpers

Optional infrastructure examples for self-hosting morning-signal on a
Linux box with systemd.

## Freshness watchdog

`generate` runs under an in-process notifier (flow-doctor → Telegram) that
reports any exception raised *inside* the run. But some failures page nobody:

- a **bootstrap/credential failure** — AssumeRole or SSM load happens *before*
  the Telegram creds are loaded, so a failure there is silent;
- the **generate timer never firing** — no process, nothing to report;
- an **OOM kill** — `SIGKILL` can't run a handler.

The watchdog closes that gap by checking the *deliverable* instead of the
process: `morning-signal watchdog` verifies today's episode object is present
and fresh in S3, exits non-zero otherwise, and (with `--notify`) sends a
Telegram alert. Run it on a timer a little after your generate slot.

```sh
sudo cp morning-signal-watchdog.service morning-signal-watchdog.timer /etc/systemd/system/
# edit User=, the venv path, and OnCalendar to match your install
sudo systemctl daemon-reload
sudo systemctl enable --now morning-signal-watchdog.timer
```

Verify: `systemctl list-timers morning-signal-watchdog.timer` and
`morning-signal watchdog` (manual run; exit 0 = today's episode is present).
