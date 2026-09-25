#!/usr/bin/env python
"""
setup_chitra.py — One-stop setup + diagnostic for CHITRA (Google Drive bridge).

What this script does:
  1. Detects whether Google Drive for Desktop is installed + signed in.
     If yes, CHITRA needs nothing else. Most users stop here.
  2. Validates the optional OAuth credentials file (only needed to read the
     CONTENT of Google Docs / Sheets / Slides — local mount handles every
     other file type).
  3. Runs the OAuth installed-app flow with VERBOSE errors so you don't
     hit cryptic "an error loading your OAuth app" messages with no clue
     what's wrong.
  4. Saves the token + does a smoke test (lists 1 file) to confirm.

Run with:   python setup_chitra.py
"""

import json
import os
import sys
import traceback

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
SECRETS_DIR = os.path.join(PROJECT_ROOT, "data", "secrets")
CREDS_PATH = os.path.join(SECRETS_DIR, "google_oauth_credentials.json")
TOKEN_PATH = os.path.join(SECRETS_DIR, "google_oauth_token.json")
SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]

DIVIDER = "─" * 60


def banner(text):
    print()
    print(DIVIDER)
    print(text)
    print(DIVIDER)


def step1_detect_local_mount():
    banner("STEP 1 — Local mount (zero-config path)")
    candidates = [
        r"G:\My Drive",
        r"H:\My Drive",
        r"I:\My Drive",
        os.path.expandvars(r"%USERPROFILE%\My Drive"),
        os.path.expandvars(r"%USERPROFILE%\Google Drive"),
    ]
    found = []
    for p in candidates:
        if p and os.path.isdir(p):
            found.append(p)
            try:
                count = sum(1 for _ in os.scandir(p))
                print(f"  ✓ Mounted: {p}  ({count} top-level entries)")
            except OSError:
                print(f"  ✓ Mounted: {p}  (couldn't enumerate)")
    if not found:
        print("  ✗ Not detected.")
        print()
        print("    Install Google Drive for Desktop:")
        print("      https://www.google.com/drive/download/")
        print("    Sign in. Pick Stream (default) or Mirror mode.")
        print("    Re-run this script — most users stop here. No OAuth needed.")
        return False
    print()
    print("  → CHITRA will use this path. No OAuth required for non-Google-")
    print("    native files (PDFs, images, .docx, text, code, etc.).")
    return True


def step2_check_oauth_creds_file():
    banner("STEP 2 — OAuth credentials (only needed for Google Docs content)")
    if not os.path.exists(CREDS_PATH):
        print(f"  ✗ No credentials.json at {CREDS_PATH}")
        print()
        print("  HOW TO GET IT:")
        print("    1. https://console.cloud.google.com/")
        print("    2. Select / create a project. Top-left dropdown.")
        print("    3. APIs & Services → Library → search 'Google Drive API' → Enable.")
        print("    4. APIs & Services → OAuth consent screen:")
        print("         User type: External")
        print("         App name: 'ODIN'   (anything works)")
        print("         User support email: your email")
        print("         Developer email: your email")
        print("         Skip Scopes step (default ok)")
        print("         Test users → Add YOUR Google account → Save")
        print("       (This step is what causes 'error loading your OAuth app' —")
        print("        it has to exist BEFORE you create credentials.)")
        print("    5. APIs & Services → Credentials → Create credentials")
        print("         → OAuth client ID → Desktop app → name 'ODIN' → Create.")
        print("    6. Click DOWNLOAD JSON.")
        print(f"    7. Save the file to:")
        print(f"         {CREDS_PATH}")
        print("    8. Re-run this script.")
        return False
    # Validate the JSON shape.
    try:
        with open(CREDS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        print(f"  ✗ credentials.json is invalid JSON: {e}")
        print(f"    Re-download from Google Cloud Console → Credentials.")
        return False
    if "installed" not in data and "web" not in data:
        print(f"  ✗ credentials.json doesn't look like an OAuth client.")
        print(f"    Make sure you picked 'Desktop app' (not 'Service account').")
        return False
    if "web" in data:
        print(f"  ✗ credentials.json is for a WEB application, not Desktop.")
        print(f"    Re-create with Application type: 'Desktop app'.")
        return False
    inst = data.get("installed", {})
    client_id = inst.get("client_id", "")
    project_id = inst.get("project_id", "(unknown)")
    print(f"  ✓ credentials.json valid")
    print(f"    project: {project_id}")
    print(f"    client : {client_id[:20]}...{client_id[-12:] if len(client_id) > 32 else ''}")
    return True


def step3_run_oauth():
    banner("STEP 3 — Authorize ODIN (one-time browser flow)")
    try:
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from google.auth.transport.requests import Request
        from googleapiclient.discovery import build
    except ImportError as e:
        print(f"  ✗ Google client libs missing: {e}")
        print("    Install: pip install google-api-python-client google-auth-oauthlib")
        return False

    creds = None
    if os.path.exists(TOKEN_PATH):
        try:
            creds = Credentials.from_authorized_user_file(TOKEN_PATH, SCOPES)
            print(f"  Found existing token at {TOKEN_PATH}")
        except Exception as e:
            print(f"  Existing token unreadable: {e}. Will re-auth.")
            creds = None

    if creds and not creds.valid:
        if creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
                print("  ✓ Token refreshed.")
            except Exception as e:
                print(f"  Refresh failed: {e}. Re-authorizing from scratch.")
                creds = None

    if not creds or not creds.valid:
        print("  Opening browser for Google sign-in...")
        print("  (If a browser tab doesn't open in 5 sec, copy the URL it prints.)")
        try:
            flow = InstalledAppFlow.from_client_secrets_file(CREDS_PATH, SCOPES)
            creds = flow.run_local_server(port=0, open_browser=True)
        except Exception as e:
            print(f"  ✗ OAuth flow failed.")
            print(f"    Reason: {e}")
            print()
            print("  Common causes:")
            print("    - 'Error 403: access_denied' → You aren't a Test User on the")
            print("      consent screen. Go to OAuth consent screen → Test users →")
            print("      Add your Google account → save.")
            print("    - 'redirect_uri_mismatch' → You created a 'Web' client, not")
            print("      'Desktop'. Re-create as Desktop app.")
            print("    - 'an error loading your OAuth app' → The consent screen")
            print("      isn't configured. Complete it (Step 2 above) FIRST.")
            traceback.print_exc()
            return False
        try:
            with open(TOKEN_PATH, "w", encoding="utf-8") as f:
                f.write(creds.to_json())
            print(f"  ✓ Token saved to {TOKEN_PATH}")
        except OSError as e:
            print(f"  ✗ Couldn't save token: {e}")
            return False

    # Smoke test: list 1 file.
    try:
        service = build("drive", "v3", credentials=creds, cache_discovery=False)
        resp = service.files().list(
            pageSize=1, fields="files(id, name, mimeType)",
        ).execute()
        files = resp.get("files", [])
        if files:
            f = files[0]
            print(f"  ✓ API works. Sample file: '{f['name']}' ({f.get('mimeType','?')})")
        else:
            print("  ✓ API responds. (Your Drive is empty or this account has no files.)")
        return True
    except Exception as e:
        print(f"  ✗ Drive API smoke test failed: {e}")
        return False


def main():
    print("CHITRA setup — ODIN's Google Drive bridge")
    os.makedirs(SECRETS_DIR, exist_ok=True)

    local_ok = step1_detect_local_mount()
    creds_ok = step2_check_oauth_creds_file()

    if not creds_ok:
        if local_ok:
            print()
            print(DIVIDER)
            print("DONE — you can stop here.")
            print(DIVIDER)
            print("CHITRA is fully functional via the local mount. PDFs, images,")
            print(".docx, code, plain text — all reachable. Just say:")
            print('   "search my drive for X"')
            print('   "ask my drive about Y"')
            print('   "what did I work on this week"')
            print()
            print("Only set up OAuth if you specifically need the CONTENT of Google")
            print("Docs / Sheets / Slides (those live in the cloud only, not on disk).")
            return 0
        print()
        print("Neither path is configured yet. Set up the local mount (recommended)")
        print("or the OAuth credentials (advanced). See instructions above.")
        return 1

    api_ok = step3_run_oauth()
    print()
    print(DIVIDER)
    if local_ok and api_ok:
        print("DONE — both paths live. CHITRA is at full capability.")
    elif api_ok:
        print("DONE — API path live. CHITRA can read Google Docs content.")
        print("Recommend: also install Google Drive for Desktop for offline reads.")
    elif local_ok:
        print("Partial — local mount works, API path failed. See errors above.")
        return 1
    else:
        print("Nothing is working yet. Review errors above.")
        return 1
    print(DIVIDER)
    return 0


if __name__ == "__main__":
    sys.exit(main())
