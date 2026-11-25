# pyWatts - indoor trainer ERG app

Python >=3.11, PyQt6, pyqtgraph, bleak, platformdirs, sqlite3, fit-tool, pytest(-qt), ruff.
Scope: workout editor, ERG rides over FTMS/BLE, HR straps, BLE power meters, history, profile, progress, Strava upload, ride import (files; Wahoo and Garmin, automatic), training plan with AI coach. No video, other cloud, ANT+. Riders on one device: own data, shared library that everyone may edit, no passwords.

## Commands (venv in .venv; POSIX: .venv/bin/python)
- `.venv\Scripts\python -m pip install -e .[dev]`
- `.venv\Scripts\python -m pytest` (offscreen set in tests/conftest.py)
- `.venv\Scripts\python -m ruff check . ; .venv\Scripts\python -m ruff format .`
- `.venv\Scripts\pywatts --fake [--db PATH]`; hardware check: `python scripts/ble_spike.py` (`--report` for unknown devices)
- Ubuntu: `bash scripts/install_linux.sh`. One codebase, no Linux branch. Tests in WSL: copy the project into the WSL home, never share `.venv`.

## Architecture
- `core/` workout, zwo, runner, metrics, zones. Imports NO Qt, bleak, sqlite, nor other pywatts packages.
  `core/plan.py`: units, status, matching with rides, planned load (pure).
  `core/power_match.py`: TrainerControl between Runner and bridge, corrects the target (off by default).
- `trainer/` base (Protocol), ftms (bleak), sensor (base of hr and power), cycling_power (0x2A63 parser, pure), radio, fake (simulator), thread (TrainerBridge). Only thread.py uses Qt.
- `storage/` db, repo (+SessionRecorder), profile (rider + zones), fit_export, fit_import, import_files. Imports core only; no Qt.
  `storage/riders.py`: list, add, remove riders (`<stem>-riders/<id>.db`, table library.riders).
  `storage/plan_repo.py`: plans, units, ride links, undo log; shares Repo's connection.
- `sync/` strava + oauth + wahoo + garmin + base (httpx, keyring; no Qt), thread (SyncBridge), service (rules, the only Repo user), gui_gc. Imports storage/core, not ui.
- `ai/` tools (no Qt) + server (QLocalServer) + service (rules) + mcp_stdio (own process: no Qt, no DB); pty + launch (program in the terminal), prompt.
- `ui/` thin widgets; `ride_controller.py` joins Runner + bridge + recorder.
- `app.py` wiring/CLI; run_riders: one window per rider, everything is torn down and rebuilt on a switch. `platform.py` only OS-specific seam (data dirs, signals, bluetoothctl).
- No pyftms, no qasync: Qt makes the GUI thread STA, which breaks bleak WinRT.

## Threads
- BLE: one thread + asyncio loop owned by TrainerBridge; GUI talks to it via methods/signals only.
- Strap and power meter have their own tasks in the BLE loop (bridge.hr_*, pm_*), so they never delay trainer control.
- `trainer/radio.py`: one scan/find/connect at a time (BlueZ: InProgress), one Radio per bridge, on every OS. Connecting the trainer preempts sensors, whose operation is repeated, so it must clean up when cancelled. Control, halt and disconnect never go through it.
- Sync: one worker thread (network, FIT encode/decode), idle during rides. No DB there. GuiGc keeps garbage collection on the GUI thread: collecting Qt objects on a worker crashes.
- AI program: PTY reader/writer threads in ai/pty.py, parsed on the GUI thread in time slices (a flood must not delay the 1 Hz tick). Also holds GuiGc.
  Never drop a pywinpty PTY whose child is still alive: it can block thread creation in the whole process.
- Everything else (Runner, Repo, widgets) lives on the GUI thread. Never block it.

## UI contracts
- `EditorPage(repo)`: signal `ride_requested(object)` (Workout); `refresh()`
- `ProfilePage(repo)`: `profile_changed`, `confirm_discard()`; `ProgressPage(repo)`: `metrics_changed`, `refresh()` reloads only while shown (guessed loads read all samples); `theme.py`: dark theme, ZONE_COLORS, `set_role` (variants: primary, danger, card, chip...); shared widgets in `ui/widgets/common.py`
- `SettingsPage(repo, bridge)`: signal `settings_changed()`
- `RideController(repo, bridge, clock=time.monotonic, parent=None)` QObject: `load(workout)`, `start/pause/resume/skip/extend/stop()`, `runner`, 1 Hz QTimer calling public `tick()`; signals `updated()`, `state_changed(object)`, `session_finished(int)`, `error(str)`
- `RidePage(repo, bridge, controller)`: `set_workout(workout)`; `ConnectDialog(repo, bridge)`, HrDialog, PowerMeterDialog. Its top bar must fit a 1366 px screen: keep texts short.
- `RideController.reload_settings()`: power matching, also during a ride; `match_offset_w`
- `SyncPage(service)` with `ui/widgets/source_box.py` per service; `MainWindow(..., sync=None)` and `HistoryPage(repo, sync=None)` work without sync.
- `PlanPage(repo, plans, coach_panel=None)`: `ride_requested(Workout, unit_id)`, `workout_requested(UUID)`, `refresh()` (reloads only while shown). `CoachPanel(repo, plans, coach)`: `undone`, `shutdown()`.
- `MainWindow(..., riders=None)`: `ui/riders.RiderButton`; `switch_rider` closes the window, app.py reads `next_rider`. Never during a ride.
- `MainWindow(..., coach=None)`: no terminal without it. Unit colors: theme.PLAN_COLORS, states in ui/widgets/calendar_model.py.
- `HistoryPage`: `refresh()`; `ui/widgets/profile_plot.ProfilePlot`: set_profile/set_cursor/set_trace

## Conventions
- pathlib everywhere, UTC in DB, power as FTP fraction in models and watts at the edges.
- Parsers are pure functions with byte fixtures; timing code takes an injectable clock.
- Five zones (core/zones.py). Tracked values (repo.TRACKED) change only via record_metric/save_profile, so they are logged.
- samples.segment +1 per pause; peaks (session_peaks) never span segments.
- One DB file per rider; `workouts` is in a second file, attached as `library` (db.open_db; default `<stem>-library.db`). Never create a `workouts` table in the rider file: it would hide the library's.
- Migrations are append-only (db.MIGRATIONS, db.LIBRARY_MIGRATIONS). `backfill_analysis()` at startup analyses rides of older versions.
- An exception escaping a Qt slot aborts the app: wrap slot work in `_guard`. Dialogs are overridable methods, for tests.
- A tick gap > 10 s (sleep) auto-pauses and is not credited; trainer loss auto-pauses, strap and power meter loss do not. Strap HR beats trainer HR; power and cadence of the meter beat the trainer's while fresh (samples.power = the value used).
- Startup: pages must not read samples when built; fit_tool is imported on use. Windows throttles a process without a visible window (6x slower), so `platform.set_full_speed` holds from start until the window is shown. Measure launches from Explorer: a terminal does not show it.
- Lists filled and selected before they are shown: `ensurePolished()` first, else rows are measured without the stylesheet (Linux).
- Sessions snapshot the workout; inserted as `crashed`, updated at finish; flush ~10 s.

## Sync rules
- Strava is upload only: its API policy forbids storing data read through it. Secrets only in the OS keychain, never in DB, files or logs. One keychain entry per rider (app.strava_account): the default database keeps the original one, every other uses its rider_id.
- Strava title: `<workout> with pyWatts` (service.activity_name), imported rides keep their name.
- Imported rides (sessions.source != 'pywatts') are never auto-uploaded. `imports` rows outlive their session: deleted rides stay away.
- Services (base.ActivitySource, repo.SERVICES): fetched START_MS after the source reports connected, FIRST_DAYS back, later checked - OVERLAP_DAYS. Every fetch() ends with one FetchEnd; rate limits end it, the 5 min timer repeats it. `download` returns bytes, so that the gate is passed before decoding. Results of a cancelled job are acknowledged and dropped.
- Rides from an API are told from files by `imports` (external_id not `sha256:`). `forget_service` is the one exception to "imports outlive their session": Wahoo demands deletion.
- Wahoo: PKCE, scopes only user_read workouts_read (a scope the app lacks in the portal = invalid_scope; offline_data is for webhooks), refresh needs the verifier too, https redirect only (so the address is pasted), list newest first without date filter, no rides of third-party apps, sandbox 25/5 min, 100/h, 250/day, at most 10 tokens per user. Store the new refresh token before using the new access token. Never send the token to the FIT CDN. Agreement: no passing on (hence the AI consent), delete on withdrawal, privacy policy (PRIVACY.md), attribution.
- Garmin Personal API (since 2026-10): key of the rider, exchanged for a JWT. Only paths and login are documented, so the parser is tolerant; own use only, never a shared key.

## AI coach rules
- Writes go only through PlanRepo.apply("ai", ...): one transaction, logged, undoable. The AI must not change workouts of origin 'user' (enforced in PlanRepo; saving in the editor makes a workout the user's), nor those the AI made for another rider (workouts.owner = rider_id). Rides, profile, FTP: read-only.
- Rides fetched from a service are hidden from all tools (Tools._hidden, read at every call) unless the rider allowed it (`<service>_ai`); units linked to them are `unknown`. A new tool that shows rides must filter too.
- No trainer or ride control, SQL, secrets, device addresses or paths in tools. All calls are refused during a ride.
- Claude Code starts with --strict-mcp-config --restricted --tools "" (verified 2026-11-24 with 2.1.330: only pyWatts tools offered). Never drop a flag to make a start work. Token only in the environment. Consent first, never auto-started.
- A ridden unit keeps the workout as ridden; others follow the library. Links removed by hand are never matched again.

## ERG safety (do not regress; tests/trainer/test_ftms_trainer.py)
- Never leave load on: halt before disconnect and after reconnect if not running; refused halt -> retry -> min target -> error.
- BlueZ keeps the link, and the load, when a process dies: SIGTERM/SIGINT/SIGHUP leave the Qt loop so that run_rider halts (platform.on_termination, tests/test_termination.py). After kill -9 find_device ends the left-over link, only if the device was not found.
- PowerMatch: difference of averages, no integral; <= 20 % and 50 W, 5 W per 5 s; nothing while paused, stopped or open; back to 0 if the meter is silent for 30 s.
- A switch of riders needs TrainerBridge.shutdown() to return True (BLE thread ended); otherwise the app ends instead.
- No target writes while paused/stopped; a failed start is retried by the resend loop. bleak WinRT raises CancelledError when Windows aborts an operation: handle with `reraise_own_cancel`. Bridge: disconnect cancels a pending connect by design.

## FTMS
- Svc 0x1826; features 2ACC; bike data 2AD2 notify; control 2AD9 write+indicate; status 2ADA; HR 180D/2A37.
- Sequence: subscribe indications -> 00 request control -> 07 start -> 05 target (s16 LE W); resend every ~5 s.
- Open mode (Sprint): 11 wind s16, grade s16, crr u8, cw u8 (flat road), resent like a target; refused -> min target -> error.
- Feature bits (2ACC) are hints only, nothing is refused because of them; no power or cadence announced: the trainer's 2A63 is the fallback. Quirks of other trainers only with a `--report` as evidence.
- 08 01 stop, 08 02 pause. Response: 80, opcode, 01=success. Permission lost (status FF / result 05) -> re-request.
- Bike data: flags u16; bit0 CLEAR=speed u16 .01; then bit1 avg speed u16, bit2 cadence u16 .5, bit3 avg cad u16,
  bit4 dist u24, bit5 resistance s16, bit6 power s16, bit7 avg power s16, bit8 energy u16 u16 u8,
  bit9 HR u8, bit10 MET u8, bit11 elapsed u16, bit12 remaining u16.

## Status (2026-11-24)
- Riders: done, tested with the simulated trainer. Not yet tried: a switch with a real trainer, a second Strava account, the coach with a second rider. Deleting a workout leaves other riders' units the copy from when they were planned.
- First real ride done on Windows: 90 min ERG on a KICKR CORE 2 with a Wahoo TRACKR HR strap, no pauses, auto-finished, uploaded to Strava.
- Not yet seen on hardware: pause/resume, reconnect, sprint, Radio, any power meter, matching.
- Ubuntu 24.04 (2026-11-25): all tests, GUI (Wayland) and simulated rides pass in WSL, which has no Bluetooth. BlueZ behaviour is read from bleak's source, never seen. Checklist in README; experimental until then. macOS untested.
- Progress plots (power curve with hover and W/kg, fitness and form, weekly load, trends) and PNG export: done (ui/widgets/power_curve.py). Data is in session_peaks, sessions (np, load, weight_kg), body_metrics. Estimated loads of rides without power are never stored.
- Limits: threshold HR, height, sex stored but unused; FIT export has no pauses.
- Open mode (0x11) never tried on a real trainer; power after a sprint returns to ERG without ramp-in.
- Sync: Strava upload and Wahoo file import work with real data; Zwift files untested. Wahoo and Garmin clients (2026-11-25) are tested against simulated servers only: run `scripts/wahoo_spike.py` (sandbox app approved) and `scripts/garmin_spike.py`, then compare with the parsers.
- Training plan: tested with the real Claude Code program against a fake API, not yet with a real model. POSIX terminal only tried by script, macOS never.
