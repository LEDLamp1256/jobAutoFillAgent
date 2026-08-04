#!/usr/bin/env python3
"""
Usage:
    python login.py --url "https://app.joinhandshake.com/login" --output handshake_auth.json

Opens a real, headed browser and pauses. Log in by hand -- SSO, 2FA,
whatever your school requires -- then resume from the Playwright
Inspector once you can see your dashboard/search page. The authenticated
session (cookies + localStorage) gets saved to --output.

navigator.py can then load that file via --auth-state so every new
browser context it creates starts already logged in, instead of needing
a fresh manual login every time one gets created.

SECURITY NOTE: the output file contains live session cookies -- treat it
like a password. Don't commit it to source control, don't share it.
Sessions expire eventually (exact lifetime depends on the site); if
navigator.py starts silently returning zero listings again after
previously working, re-run this to refresh the session.
"""

import argparse
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright


def login(url: str, output_path: str) -> None:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context()
        page = context.new_page()

        try:
            page.goto(url, wait_until="domcontentloaded")
        except Exception as e:
            print(f"[FATAL] Could not load {url}: {e}")
            browser.close()
            sys.exit(1)

        print("\n" + "=" * 70)
        print("Log in manually in the browser window that just opened.")
        print("Once you're fully logged in and can see your dashboard or")
        print("search results, come back here and click 'Resume' in the")
        print("Playwright Inspector to save your session.")
        print("=" * 70 + "\n")

        page.pause()  # blocks here until you resume, after logging in by hand

        context.storage_state(path=output_path)
        print(f"\nSaved authenticated session to: {output_path}")
        print(f"Use it with: python navigator.py --auth-state {output_path} ...")
        print("(Re-run this script later if the saved session expires.)")

        browser.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="One-time manual login capture for auth-gated job boards."
    )
    parser.add_argument("--url", required=True, help="Login page URL to open.")
    parser.add_argument(
        "--output", default="auth_state.json", help="Where to save the session state."
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    out_path = Path(args.output)
    if out_path.exists():
        confirm = input(
            f"{out_path} already exists and will be overwritten. Continue? [y/N]: "
        ).strip().lower()
        if confirm != "y":
            print("Aborted.")
            sys.exit(0)
    login(args.url, str(out_path))