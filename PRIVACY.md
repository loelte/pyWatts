# Privacy

pyWatts runs on your computer. It has no server, no accounts of its own, no analytics and no advertising. Its developers never receive your data.

## What is stored, and where

- Rides, profile, settings and plans are in database files in your user folder (Windows: `%LOCALAPPDATA%`). They are not encrypted by pyWatts: use the disk encryption of your system (BitLocker, LUKS) and a system login.
- Logins of Strava, Wahoo and Garmin are only in the keychain of your system, never in the database, files or logs. Each rider has their own.
- Riders on the same computer have separate files, but there are no passwords: whoever uses your system login can see all of them.

## What is sent, and to whom

Nothing is sent before you connect a service or start the AI coach.

| Service | What happens |
|---|---|
| Strava | Rides you upload, by hand or automatically if you switch that on, go to your Strava account. Nothing is read from Strava. |
| Wahoo | pyWatts reads the list of your workouts and downloads the files of your cycling workouts (time, power, heart rate, cadence, speed, altitude). Positions in the files are not stored. Nothing is written to your account. |
| Garmin | The same, with the personal key you created. |
| AI coach | When you press Start, the AI program sends data to its provider: rides, profile and plans, see README. Rides fetched from Wahoo or Garmin are left out unless you allow it for that service on the Sync page. |

Data fetched from Wahoo or Garmin is used only to show and analyse your training on your computer. It is not sold, shared or used for advertising.

## Your choices

- **Stop:** Sync > Disconnect deletes the login. For Wahoo it also asks Wahoo to withdraw the access, if it can be reached. A Garmin key stays valid until you delete it in Garmin Connect.
- **Delete:** when you disconnect, pyWatts offers to delete all rides fetched from that service. Single rides can be deleted in History. A backup you made yourself is not changed.
- **Take along:** every ride can be exported as a `.fit` file in History.
- **Remove a rider:** deletes their logins; the file is kept in `pywatts-riders/removed` until you delete it.

Your rights towards Strava, Wahoo, Garmin and the AI provider are described in their own privacy policies.

Questions: open an issue at https://github.com/loelte/pyWatts/issues (it is public, so please no personal data).
