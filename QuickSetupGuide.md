# pyWatts – Quick Setup Guide

pyWatts is an indoor-trainer ERG app: workout editor, ERG rides over Bluetooth (FTMS), heart-rate straps and power meters, history, progress, ride import and Strava upload, a training plan with an optional AI coach, and several riders on one computer.

> pyWatts runs on **Windows**. **Ubuntu 24.04 or newer is experimental**: it has not been tried with a real trainer yet. See the [README](README.md) for the full feature description.

## 1. What you need

- Windows 10 or 11 (or Ubuntu 24.04+) with Bluetooth (LE) built in or a USB dongle
- Python 3.11 or newer (Windows: from [python.org](https://www.python.org/downloads/); tick "Add python.exe to PATH". Ubuntu 24.04 already has it)
- A smart trainer that supports FTMS (e.g. Wahoo KICKR) and, optionally, a BLE heart-rate strap and a BLE power meter

## 2. Install

**Windows:** Download the repository (**Code > Download ZIP**, then unzip) or clone it. Open PowerShell in the project folder:

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -e .
```

For development (tests, linting) use `-e .[dev]` instead.

**Ubuntu** (do not reuse a project folder from Windows). pyWatts runs from the project folder, so keep it:

```bash
sudo apt install git python3-venv libxcb-cursor0 libegl1 libxkbcommon-x11-0 bluez
git clone https://github.com/loelte/pyWatts.git ~/pyWatts
cd ~/pyWatts
bash scripts/install_linux.sh
```

In the rest of this guide replace `.venv\Scripts\` by `.venv/bin/`.

## 3. Try it without hardware

```powershell
.venv\Scripts\pywatts --fake
```

This starts the app with a simulated trainer. Use it to look around, build a workout and test a ride.

## 4. Start it with your trainer

1. Turn on Bluetooth (Windows: Settings → Bluetooth & devices).
2. Wake the trainer by pedalling a few turns. Close other apps that may control it (Zwift, Wahoo app, also on phones): only one app can control a trainer.
3. Start the app:
   ```powershell
   .venv\Scripts\pywatts
   ```
4. On the ride page open the connect dialog, pick your trainer (and the HR strap and power meter), and connect.
5. Pick a workout and start. The trainer is then controlled in ERG mode.

Your data lives in the per-user data folder (SQLite database). To use a different file: `pywatts --db C:\path\to\my.db`.

## 5. First steps in the app

- **Profile**: enter your FTP and weight. Zones and workout power targets are based on them.
- **Editor**: create a workout or import a `.zwo` file.
- **Ride**: start a workout; pause, skip or extend intervals during the ride.
- **History / Progress**: finished rides are stored and can be exported as FIT.
- **Riders**: several riders can share one device; each has their own data, the workout library is shared.

## 6. Optional: Strava upload

Strava needs a subscription and your own API app: create one at strava.com/settings/api with callback domain `localhost`, then enter its client ID and secret on the Sync page and connect. Finished rides are uploaded as `<workout> with pyWatts`. Credentials are kept in the system keychain (Windows Credential Manager, GNOME Keyring), never in files.

**Several riders on one PC:** each rider has their own Strava login. When someone else takes over, press **Disconnect** on the Sync page, **log out of strava.com in the browser**, then switch rider and connect. Otherwise the browser reuses the previous person's Strava session and the rides go to the wrong account.

## 7. Optional: AI coach

The training plan page can open a terminal with Claude Code as a coach. It needs Claude Code installed and asks for your consent before it starts. It never controls the trainer and cannot start or change rides.

## 8. Troubleshooting

| Problem | Try |
|---|---|
| Trainer not found | Pedal to wake it, disconnect it from other apps and phones, toggle Windows Bluetooth off and on |
| Ubuntu: “The Bluetooth service is not running” | `sudo apt install bluez`, then `systemctl status bluetooth` |
| Ubuntu: trainer not found after a crash | Start pyWatts again and connect. If that fails: `bluetoothctl disconnect` |
| Power differs from the target | With a power meter this is normal; see Settings > Match the trainer to the power meter |
| Device is not supported | Send the output of `ble_spike.py --report` |
| Connection drops | Move the PC or dongle closer; use a USB extension cable for the dongle |
| `python` not found | Reinstall Python with "Add to PATH", or use `py -3.11` |
| Check the hardware alone | `.venv\Scripts\python scripts\ble_spike.py` |
| More log output | `.venv\Scripts\pywatts -v` |

## 9. Developers

```powershell
.venv\Scripts\python -m pytest
.venv\Scripts\python -m ruff check .
.venv\Scripts\python -m ruff format .
```
