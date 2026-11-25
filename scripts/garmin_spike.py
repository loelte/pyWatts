"""Garmin Personal API check: exchange the key, list one page of activities, read one file.

python scripts/garmin_spike.py

Uses 3 requests. Stores nothing and never prints keys, tokens or ride contents, only field names
and counts. The key is made in Garmin Connect: see KEY_PAGE in src/pywatts/sync/garmin.py.
"""

from __future__ import annotations

import getpass
import sys

from pywatts.storage.import_files import read_archive
from pywatts.sync.base import MemoryStore, SyncError, http_client
from pywatts.sync.garmin import API, KEY_PAGE, PER_PAGE, SEARCH, GarminClient


def main() -> int:
    print(f"Key page: {KEY_PAGE}")
    key = getpass.getpass("API key (hidden): ").strip()
    with http_client() as http:
        client = GarminClient(MemoryStore(), http)
        try:
            client.connect(key)
            print(f"[OK]   key accepted, {len(key)} characters")
            headers = {"Accept": "application/json"}
            r = client._request(SEARCH, headers, {"start": 0, "limit": PER_PAGE})
            limits = {k: v for k, v in r.headers.items() if "limit" in k.lower()}
            print(f"       list: HTTP {r.status_code}, {type(r.json()).__name__}, limits {limits}")
            if isinstance(r.json(), dict):
                print(f"       fields {sorted(r.json())}")
            page = client.page(1, None)
            print(f"[OK]   {len(page.rides)} activities, more {page.more}")
            dated = sum(a.started_at_utc is not None for a in page.rides)
            named = sum(bool(a.title) for a in page.rides)
            kinds = [a.cycling for a in page.rides]
            counts = {str(k): kinds.count(k) for k in (True, False, None)}
            print(f"       with start {dated}, with title {named}, cycling {counts}")
            rows = r.json() if isinstance(r.json(), list) else []
            if rows and isinstance(rows[0], dict):
                print(f"       activity fields {sorted(rows[0])}")
            starts = [a.started_at_utc for a in page.rides if a.started_at_utc]
            print(f"       newest first {starts == sorted(starts, reverse=True)}")
            chosen = next((a for a in page.rides if a.cycling), None)
            if chosen is None:
                print("[FAIL] no cycling activity on the first page")
                return 1
            data = client.download(chosen)
            if data is None:
                print("[FAIL] no file")
                return 1
            result = read_archive("garmin", data)
            samples = len(result.ride.samples) if result.ride else 0
            power = sum(s.power is not None for s in result.ride.samples) if result.ride else 0
            print(f"       file: {len(data)} bytes, zip {data.startswith(b'PK')}")
            print(f"       ride: {samples} s, {power} s with power, {result.error or 'readable'}")
        except SyncError as e:
            print(f"[FAIL] {e} ({API})")
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
