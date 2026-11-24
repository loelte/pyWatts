"""Wahoo account check: log in, list one page of workouts, read one FIT file, refresh the token.

python scripts/wahoo_spike.py [--redirect https://localhost:8723/callback]

Uses at most 4 API requests. Stores nothing and never prints tokens or ride contents.
"""

from __future__ import annotations

import argparse
import base64
import getpass
import hashlib
import secrets
import sys
import webbrowser
from urllib.parse import parse_qs, urlencode, urlsplit

from pywatts.storage.import_files import read_bytes
from pywatts.sync.base import http_client

BASE = "https://api.wahooligan.com"
SCOPE = "user_read workouts_read offline_data"


def login_code(client_id: str, redirect: str, challenge: str) -> str:
    state = secrets.token_urlsafe(16)
    query = {
        "client_id": client_id,
        "redirect_uri": redirect,
        "scope": SCOPE,
        "response_type": "code",
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    webbrowser.open(f"{BASE}/oauth/authorize?{urlencode(query)}")
    print("Log in. The page after that cannot be loaded; copy its full address.")
    answer = parse_qs(urlsplit(getpass.getpass("Paste the address (hidden), Enter: ")).query)
    if answer.get("state") != [state]:
        sys.exit("[FAIL] the address does not belong to this login")
    if "code" not in answer:
        sys.exit("[FAIL] access was not granted")
    print("[OK]   login")
    return answer["code"][0]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--redirect", default="https://localhost:8723/callback")
    redirect = parser.parse_args().redirect
    client_id = input("Wahoo client ID: ").strip()
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).decode().rstrip("=")
    code = login_code(client_id, redirect, challenge)
    with http_client() as http:

        def token(**data: str) -> dict:
            form = {"client_id": client_id, "code_verifier": verifier, **data}
            r = http.post(f"{BASE}/oauth/token", data=form)
            if r.status_code != 200:
                sys.exit(f"[FAIL] {data['grant_type']}: HTTP {r.status_code}")
            body = r.json()
            print(f"[OK]   {data['grant_type']}: fields {sorted(body)}")
            return body

        def get(path: str, access: str) -> dict:
            r = http.get(BASE + path, headers={"Authorization": f"Bearer {access}"})
            limits = {k: v for k, v in r.headers.items() if "ratelimit" in k.lower()}
            print(f"       {path.split('?')[0]}: HTTP {r.status_code}, limits {limits}")
            if r.status_code != 200:
                sys.exit("[FAIL] request refused")
            return r.json()

        tokens = token(grant_type="authorization_code", code=code, redirect_uri=redirect)
        page = get("/v1/workouts?page=1&per_page=30", tokens["access_token"])
        workouts = page.get("workouts", [])
        full = [w for w in workouts if w.get("workout_summary")]
        print(f"[OK]   {page.get('total')} workouts; {len(full)}/{len(workouts)} with summary")
        if workouts:
            chosen = (full or workouts)[0]
            detail = get(f"/v1/workouts/{int(chosen['id'])}", tokens["access_token"])
            summary = detail.get("workout_summary") or {}
            url = (summary.get("file") or {}).get("url")
            print(f"       summary fields {sorted(summary)}")
            if url:
                r = http.get(url)  # no token: this is not the API host
                result = read_bytes("wahoo.fit", r.content)
                samples = len(result.ride.samples) if result.ride else 0
                print(
                    f"       FIT: HTTP {r.status_code}, {samples} s, {result.error or 'readable'}"
                )
            else:
                print("[FAIL] no FIT link")
        if "refresh_token" in tokens:
            fresh = token(grant_type="refresh_token", refresh_token=tokens["refresh_token"])
            get("/v1/user", fresh["access_token"])
            print("[OK]   refreshed token works")
    return 0


if __name__ == "__main__":
    sys.exit(main())
