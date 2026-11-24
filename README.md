# pyWatts

Build structured workouts, ride them in ERG mode on an FTMS smart trainer over Bluetooth LE, and keep a local history with `.fit` export, `.fit` import and Strava upload. Developed for Windows 11 + Wahoo KICKR CORE 2.

> **Platform:** pyWatts currently supports **Windows 10/11 only**. A Linux version is planned and will follow soon. Everything runs locally on your machine; there is no account or server.

New here? Follow the **[Quick Setup Guide](QuickSetupGuide.md)** for a step-by-step start.

## Install and run (Windows)

1. Download this repository (green **Code** button > **Download ZIP**, then unzip) or clone it.
2. Install Python 3.11 or newer from [python.org](https://www.python.org/downloads/) (tick "Add python.exe to PATH").
3. In PowerShell, inside the project folder:

```powershell
py -m venv .venv
.venv\Scripts\python -m pip install -e .
.venv\Scripts\pywatts --fake    # simulated trainer, separate database
.venv\Scripts\pywatts           # real trainer
```

Developers: install with `-e ".[dev]"` to get pytest and ruff.

Options: `--db PATH` (use another database file), `-v` (debug log).

Workouts are in their own file next to the database (`pywatts-library.db`), other riders in the folder `pywatts-riders`. To back up, close pyWatts and copy all of them.

## First ride

1. Profile: set your FTP, and max heart rate for heart rate colors.
2. Library: create a workout, or import a `.zwo`, and press Ride.
3. Ride: Connect, pick the trainer, pedal, Start.

## Several riders

- Click the name at the top of the sidebar to add, switch or remove riders. With two or more, pyWatts asks at startup who is riding.
- Rides, profile, plan, settings and the Strava login belong to one rider. The workout library is shared and everyone can edit it. There are no passwords.
- A new rider starts with your trainer and units, and should set their FTP first. The trainer reconnects after a switch.
- Strava, when several riders share a PC: each rider connects their own Strava account. Before connecting, press **Disconnect** on the Sync page if the previous login is still there, and **log out of strava.com in the browser**, otherwise the browser signs in the previous person's account and the ride would go to the wrong profile. Disconnecting removes only the current rider's login from the keychain.
- A removed rider's file is kept in `pywatts-riders/removed`; moving it back to `pywatts-riders` brings the rider back.

## Zones, progress, heart rate strap

- Five zones color workouts and the heart rate field: Z1 grey, Z2 blue, Z3 green, Z4 orange, Z5 red. Power limits are 55/75/90/105 % of FTP, heart rate limits 60/70/80/90 % of max (or of the reserve); both can be set by hand in Profile.
- Progress shows best power from 3 s to 60 min and logs weight, resting and max heart rate and FTP over time. Efforts never span a pause.
- Sprint segments switch ERG off (simulated flat road): shift up and go all out. The trainer returns to ERG with the next block.
- Before a sprint and every block that reaches Z3, the ride page counts down the last 10 s of the block before it.
- Strap: put it on (moisten the contacts), then Ride > HR strap > Scan. Made for Wahoo TICKR/TRACKR; any strap with the standard Bluetooth heart rate service should work. It replaces the trainer's heart rate while it delivers data. Do not pair it in Windows.

## Strava and older rides

- Strava needs a subscription and your own API app: create one at strava.com/settings/api with callback domain `localhost`, then enter its client ID and secret on the Sync page. They and the login are kept in the system keychain (Windows Credential Manager), not in the database. Each database (`--db`) has its own login.
- History > Upload to Strava sends one ride. Automatic upload of completed rides is off until you enable it on the Sync page. Strava's default visibility applies. Rides are titled `<workout> with pyWatts`; imported rides keep their name.
- Sync > Import rides reads `.fit`, `.fit.gz`, folders and `.zip` archives (cycling only) and skips rides it already knows. Uploads and imports wait while you ride.
- Zwift files: `%LOCALAPPDATA%\Zwift\Activities` or `Documents\Zwift\Activities`, or download them from zwift.com.

## Training plan and AI coach

- Training Plan shows a year, month or week. Planned indoor units are light blue, outdoor units dark blue, finished ones green, missed ones red. Click a unit to open it, double-click a day to add one, drag to move.
- Rides started from a unit are linked to it. Other rides and imports are linked if the day and the duration or load (within 30 %) fit only one unit; links can be changed by hand.
- The AI coach runs in the terminal beside the calendar. Install [Claude Code](https://code.claude.com/docs), run `claude` once to log in, then press Start. Press Enter to send, Shift+Enter for a new line.
- The coach sees rides, power, heart rate, name, age, weight and other profile data, which go to its provider. It can create and change plans, units and its own workouts. It cannot change your own workouts (it copies them), rides, profile or FTP, and has no access to the trainer, files or the command line. Changes lists what it did, with Undo.
- Settings can name another program that supports MCP. That one keeps its own tools, e.g. file access, and must pass the variables `PYWATTS_COACH_PIPE` and `PYWATTS_COACH_TOKEN` on to its MCP servers.

## Before connecting: free the trainer

A trainer accepts several Bluetooth connections, but only **one app may control it**.

- Fully close the Wahoo app, Zwift, TrainerRoad, Rouvy, etc. on every phone, tablet and PC. On phones, swipe the app away or turn Bluetooth off.
- Disconnect the trainer from bike computers.
- Do **not** pair the trainer in Windows Bluetooth settings. If you already did, remove it there.
- If control is still refused, unplug the trainer for 10 seconds.

## Hardware check

With Bluetooth on and the trainer awake (pedal a few turns):

```powershell
.venv\Scripts\python scripts\ble_spike.py
```

It scans, connects, reads features, requests control, sets 150 W, prints telemetry for 30 s and stops. Options: `--addr`, `--watts`, `--seconds`.

Please report back:

1. The complete console output, including every `[OK]` / `[FAIL]` line and any traceback.
2. Whether the resistance changed to about 150 W, and after how many seconds.
3. Whether cadence, speed and heart rate show values or stay empty.
4. Whether resistance was released at the end.
5. Trainer model, firmware version (Wahoo app), Windows version, and other apps running.

Device addresses in the output identify your hardware; mask them if you post it publicly.

## Linux / macOS

Not supported yet. A Linux version is coming soon; the code is written to be portable, but it has never been tried with a trainer there, and macOS is not planned for now.

## Development

```powershell
.venv\Scripts\python -m pytest
.venv\Scripts\python -m ruff check .
```

Architecture and conventions: see [CLAUDE.md](CLAUDE.md).
