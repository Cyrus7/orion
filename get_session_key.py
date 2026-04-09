#!/usr/bin/env python3
"""
get_session_key.py — one-time helper to obtain a *permanent* Last.fm session key.

Usage:
  # fastest: open browser, poll until you click "Allow"
  ./get_session_key.py --open --poll

  # or: print URL, paste the ?token=... yourself
  ./get_session_key.py --token dca082e08d983a6e37938e7f91bcb0c2

Environment:
  LASTFM_API_KEY        (required)
  LASTFM_API_SECRET     (required)
  LASTFM_PASSWORD       (optional; used only on very old pylast that
                         requires a password hash for get_session_key)
Options:
  --open                open the auth URL in your browser
  --poll                use pylast.SessionKeyGenerator.get_web_auth_session_key(url)
                        if available (auto-polls until approved)
  --token TOKEN         paste the token from the address bar (after you clicked Allow)
  --save PATH           where to store the session key (default: ~/.session_key)
  --no-password         do NOT prompt for password; fail if old pylast requires it
"""

import os, sys, time, textwrap, getpass, argparse
try:
    import pylast
except ImportError:
    sys.exit("pylast is not installed. Install python3-pylast or pip install pylast")

DEF_SAVE = os.path.join(os.path.expanduser("~"), ".session_key")

def env(key):
    v = os.getenv(key)
    if not v:
        sys.exit(f"Set {key} in your environment.")
    return v

def main():
    ap = argparse.ArgumentParser(description="Obtain a permanent Last.fm session key")
    ap.add_argument("--open", action="store_true", help="Open the auth URL in your browser")
    ap.add_argument("--poll", action="store_true", help="Auto-poll for approval if supported")
    ap.add_argument("--token", help="Paste the ?token=... value from the address bar")
    ap.add_argument("--save", default=DEF_SAVE, help=f"File to save the session key (default: {DEF_SAVE})")
    ap.add_argument("--no-password", action="store_true", help="Do not prompt for password (older pylast may fail)")
    args = ap.parse_args()

    API_KEY    = env("LASTFM_API_KEY")
    API_SECRET = env("LASTFM_API_SECRET")

    network = pylast.LastFMNetwork(API_KEY, API_SECRET)
    skg     = pylast.SessionKeyGenerator(network)

    auth_url = skg.get_web_auth_url()
    print("\nAuthorize access at this URL:\n")
    print(textwrap.fill(auth_url, 100), "\n")

    if args.open:
        try:
            import webbrowser
            webbrowser.open(auth_url)
        except Exception:
            pass

    session_key = None

    # Path A: polling helper available and requested
    if args.poll and hasattr(skg, "get_web_auth_session_key"):
        print("Waiting for approval… (press Ctrl+C to abort)")
        while True:
            try:
                session_key = skg.get_web_auth_session_key(auth_url)
                break
            except pylast.WSError:
                time.sleep(1)

    # Path B: token flow
    if session_key is None:
        token = args.token
        if not token:
            token = input("Paste the token (the part after token=): ").strip()

        # Try modern signature first (token only) then fall back to old one (token + password hash)
        try:
            session_key = skg.get_session_key(token)  # newer pylast
        except TypeError:
            # Old pylast (e.g. 5.2) requires password hash
            if args.no_password:
                sys.exit("This pylast needs a password hash; re-run without --no-password or set LASTFM_PASSWORD.")
            pw = os.getenv("LASTFM_PASSWORD")
            if pw is None:
                pw = getpass.getpass("Your Last.fm password (only to create the hash, not stored): ")
            session_key = skg.get_session_key(token, pylast.md5(pw))

    # Save + print
    with open(args.save, "w") as f:
        f.write(session_key)
    print(f"\n✅ Session key obtained and saved to {args.save}\n")
    print("Session key:", session_key)
    print("\nAdd this to your environment (or crontab):")
    print(f"  export LASTFM_SESSION_KEY='{session_key}'\n")

    # Sanity check: can we do a write call with just the session key?
    net2 = pylast.LastFMNetwork(API_KEY, API_SECRET, session_key=session_key)
    try:
        net2.update_now_playing("Auth Check", "Auth Check")
        print("✅ Write test passed: this key can scrobble.")
    except pylast.WSError as e:
        # Code 4 = Authentication Failed / no write permission on the API key
        msg = getattr(e, "details", str(e))
        print(f"⚠️  Last.fm refused a write call: {msg}")
        print("If the message mentions 'do not have permissions', the API key is read-only.")
        print("Use a pre-approved key or ask Last.fm staff to enable write/scrobbling on your key.")

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nAborted.")

