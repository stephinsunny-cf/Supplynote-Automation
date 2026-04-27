"""
authorize_google.py
───────────────────
Run this ONCE to generate / refresh config/token.json with both
Gmail (send) and Google Drive (upload) scopes.

Usage:
    python authorize_google.py

A browser window will open asking you to approve access.
After approval the token is saved to config/token.json and you
won't need to run this again (the token auto-refreshes).
"""

from pathlib import Path
from google_auth_oauthlib.flow import InstalledAppFlow
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request

SCOPES = [
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/drive.file",   # upload files to Drive
]

CREDS_PATH = Path("config/credentials.json")
TOKEN_PATH = Path("config/token.json")


def main():
    creds = None
    required = set(SCOPES)

    if TOKEN_PATH.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)
        # Check if existing token actually has ALL required scopes
        granted = set(creds.scopes or [])
        missing = required - granted
        if missing:
            print(f"Existing token is missing scopes: {missing}")
            print("Deleting old token and re-authorizing...")
            TOKEN_PATH.unlink()
            creds = None

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            print("Refreshing existing token...")
            creds.refresh(Request())
        else:
            print("Opening browser for Google authorization...")
            flow = InstalledAppFlow.from_client_secrets_file(str(CREDS_PATH), SCOPES)
            creds = flow.run_local_server(port=0)

        TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
        TOKEN_PATH.write_text(creds.to_json())
        print(f"Token saved to {TOKEN_PATH}")

    granted = set(creds.scopes or [])
    print("\nScopes granted:")
    for s in SCOPES:
        tick = "✓" if s in granted else "✗ MISSING"
        print(f"  {tick} {s}")

    if required.issubset(granted):
        print("\nAll done! You can now use Gmail send + Drive upload.")
    else:
        print("\nWARNING: Some scopes were not granted. Re-run this script.")


if __name__ == "__main__":
    main()
