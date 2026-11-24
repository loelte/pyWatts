# pyWatts - indoor trainer ERG app

Python >=3.11, PyQt6, pyqtgraph, bleak, platformdirs, sqlite3, fit-tool, pytest(-qt), ruff.
The owner does not want git for now: no init, no commits. Never put their email or name into files or requests.
Scope: workout editor, ERG rides over FTMS/BLE, HR straps, history, profile, progress, Strava upload, ride import (files; Wahoo planned), training plan with AI coach. No video, other cloud, ANT+. Riders on one device: own data, shared library that everyone may edit, no passwords.

## Commands (venv in .venv; POSIX: .venv/bin/python)
- `.venv\Scripts\python -m pip install -e .[dev]`
- `.venv\Scripts\python -m pytest` (offscreen set in tests/conftest.py)
- `.venv\Scripts\python -m ruff check . ; .venv\Scripts\python -m ruff format .`
- `.venv\Scripts\pywatts --fake [--db PATH]`; hardware check: `python scripts/ble_spike.py`

## Architecture
- `core/` workout, zwo, runner, metrics, zones. Imports NO Qt, bleak, sqlite, nor other pywatts packages.
  `core/plan.py`: units, status, matching with rides, planned load (pure).
- `trainer/` base (Protocol), ftms + hr strap (bleak), fake (simulator), thread (TrainerBridge). Only thread.py uses Qt.
- `storage/` db, repo (+SessionRecorder), profile (rider + zones), fit_export, fit_import, import_files. Imports core only; no Qt.
  `storage/riders.py`: list, add, remove riders (`<stem>-riders/<id>.db`, table library.riders).
  `storage/plan_repo.py`: plans, units, ride links, undo log; shares Repo's connection.
- `sync/` strava + oauth + base (httpx, keyring; no Qt), thread (SyncBridge), service (rules, the only Repo user), gui_gc. Imports storage/core, not ui.
- `ai/` tools (no Qt) + server (QLocalServer) + service (rules) + mcp_stdio (own process: no Qt, no DB); pty + launch (program in the terminal), prompt.
- `ui/` thin widgets; `ride_controller.py` joins Runner + bridge + recorder.
- `app.py` wiring/CLI; run_riders: one window per rider, everything is torn down and rebuilt on a switch. `platform.py` only OS-specific seam (data dirs).
- No pyftms, no qasync: Qt makes the GUI thread STA, which breaks bleak WinRT.

## Threads
- BLE: one thread + asyncio loop owned by TrainerBridge; GUI talks to it via methods/signals only.
- The strap has its own tasks in the BLE loop (bridge.hr_*), so it never delays trainer control.
- Sync: one worker thread (network, FIT encode/decode), idle during rides. No DB there. GuiGc keeps garbage collection on the GUI thread: collecting Qt objects on a worker crashes.
- AI program: PTY reader/writer threads in ai/pty.py, parsed on the GUI thread in time slices (a flood must not delay the 1 Hz tick). Also holds GuiGc.
  Never drop a pywinpty PTY whose child is still alive: it can block thread creation in the whole process.
- Everything else (Runner, Repo, widgets) lives on the GUI thread. Never block it.

## UI contracts
- `EditorPage(repo)`: signal `ride_requested(object)` (Workout); `refresh()`
- `ProfilePage(repo)`: `profile_changed`, `confirm_discard()`; `ProgressPage(repo)`: `metrics_changed`; `theme.py`: dark theme, ZONE_COLORS, `set_role` (variants: primary, danger, card, chip...); shared widgets in `ui/widgets/common.py`
- `SettingsPage(repo, bridge)`: signal `settings_changed()`
- `RideController(repo, bridge, clock=time.monotonic, parent=None)` QObject: `load(workout)`, `start/pause/resume/skip/extend/stop()`, `runner`, 1 Hz QTimer calling public `tick()`; signals `updated()`, `state_changed(object)`, `session_finished(int)`, `error(str)`
- `RidePage(repo, bridge, controller)`: `set_workout(workout)`; `ConnectDialog(repo, bridge)`
- `SyncPage(service)`; `MainWindow(..., sync=None)` and `HistoryPage(repo, sync=None)` work without sync.
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
- A tick gap > 10 s (sleep) auto-pauses and is not credited; trainer loss auto-pauses, strap loss does not. Strap HR beats trainer HR.
- Sessions snapshot the workout; inserted as `crashed`, updated at finish; flush ~10 s.

## Sync rules
- Strava is upload only: its API policy forbids storing data read through it. Secrets only in the OS keychain, never in DB, files or logs. One keychain entry per rider (app.strava_account): the default database keeps the original one, every other uses its rider_id.
- Strava title: `<workout> with pyWatts` (service.activity_name), imported rides keep their name.
- Imported rides (sessions.source != 'pywatts') are never auto-uploaded. `imports` rows outlive their session: deleted rides stay away.
- Wahoo: PKCE, https redirect only (so the address is pasted), no rides of third-party apps, sandbox 250 requests/day. Never send the token to the FIT CDN.

## AI coach rules
- Writes go only through PlanRepo.apply("ai", ...): one transaction, logged, undoable. The AI must not change workouts of origin 'user' (enforced in PlanRepo; saving in the editor makes a workout the user's), nor those the AI made for another rider (workouts.owner = rider_id). Rides, profile, FTP: read-only.
- No trainer or ride control, SQL, secrets, device addresses or paths in tools. All calls are refused during a ride.
- Claude Code starts with --strict-mcp-config --restricted --tools "" (verified 2026-11-24 with 2.1.330: only pyWatts tools offered). Never drop a flag to make a start work. Token only in the environment. Consent first, never auto-started.
- A ridden unit keeps the workout as ridden; others follow the library. Links removed by hand are never matched again.

## ERG safety (do not regress; tests/trainer/test_ftms_trainer.py)
- Never leave load on: halt before disconnect and after reconnect if not running; refused halt -> retry -> min target -> error.
- A switch of riders needs TrainerBridge.shutdown() to return True (BLE thread ended); otherwise the app ends instead.
- No target writes while paused/stopped; a failed start is retried by the resend loop. bleak WinRT raises CancelledError when Windows aborts an operation: handle with `reraise_own_cancel`. Bridge: disconnect cancels a pending connect by design.

## FTMS
- Svc 0x1826; features 2ACC; bike data 2AD2 notify; control 2AD9 write+indicate; status 2ADA; HR 180D/2A37.
- Sequence: subscribe indications -> 00 request control -> 07 start -> 05 target (s16 LE W); resend every ~5 s.
- Open mode (Sprint): 11 wind s16, grade s16, crr u8, cw u8 (flat road), resent like a target; refused -> min target -> error.
- 08 01 stop, 08 02 pause. Response: 80, opcode, 01=success. Permission lost (status FF / result 05) -> re-request.
- Bike data: flags u16; bit0 CLEAR=speed u16 .01; then bit1 avg speed u16, bit2 cadence u16 .5, bit3 avg cad u16,
  bit4 dist u24, bit5 resistance s16, bit6 power s16, bit7 avg power s16, bit8 energy u16 u16 u8,
  bit9 HR u8, bit10 MET u8, bit11 elapsed u16, bit12 remaining u16.

## Status (2026-11-24)
- Riders: done, tested with the simulated trainer. Not yet tried: a switch with a real trainer, a second Strava account, the coach with a second rider. Deleting a workout leaves other riders' units the copy from when they were planned.
- The offscreen platform draws list rows too close together (sidebar, picker); the real one does not.
- First real ride done on Windows: 90 min ERG on a KICKR CORE 2 with a Wahoo TRACKR HR strap, no pauses, auto-finished, uploaded to Strava.
- Not yet seen on hardware: pause/resume, reconnect, sprint. Linux and macOS untested.
- Planned: interactive progress plots + plot export. Data is in session_peaks, sessions (np, load, weight_kg), body_metrics.
- Limits: threshold HR, height, sex stored but unused; FIT export has no pauses.
- Open mode (0x11) never tried on a real trainer; power after a sprint returns to ERG without ramp-in.
- Sync: Strava upload and Wahoo file import work with real data; Zwift files untested. Wahoo API: run `scripts/wahoo_spike.py` once approved, then build the client.
- Training plan: tested with the real Claude Code program against a fake API, not yet with a real model. POSIX terminal only tried by script, macOS never. Wahoo: check its API terms on AI use before its rides reach the AI tools.
