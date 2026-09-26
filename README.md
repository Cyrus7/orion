# Orion Scrobbler

Scrapes DJ Orion's weekly Friday playlist from [djorion.fi](http://www.djorion.fi/category/radio/) and scrobbles the tracks to your [Last.fm](https://www.last.fm) account.

## Features

- Parses "Artist – Title" lines (handles `-`, `–`, `—`, `:` separators with spaces)
- Filters promo/social/navigation lines
- Handles multi-line "K – System" style artist name splits
- Distributes scrobble timestamps evenly across a Helsinki-time window (default 20:00–22:00; override with `--start HH:MM --hours N`)
- Tracks processed dates to avoid duplicate scrobbles
- `--dry-run` and `--debug` modes for safe testing

## Requirements

```
python3
requests
beautifulsoup4
pylast>=5.2
```

Install dependencies:

```bash
pip install requests beautifulsoup4 pylast
```

## Setup

### 1. Get a Last.fm API key

Register at [last.fm/api/account/create](https://www.last.fm/api/account/create). You need:
- `API Key`
- `Shared Secret`

### 2. Get a session key (one-time)

```bash
export LASTFM_API_KEY=your_api_key
export LASTFM_API_SECRET=your_api_secret

./get_session_key.py --open --poll
```

This opens your browser, waits for you to click **Allow**, then saves the session key to `~/.session_key`.

### 3. Set environment variables

Add to your shell profile (`~/.bashrc`, `~/.zshrc`, etc.):

```bash
export LASTFM_API_KEY=your_api_key
export LASTFM_API_SECRET=your_api_secret
export LASTFM_SESSION_KEY=$(cat ~/.session_key)
```

## Usage

```bash
# Scrobble the next unprocessed playlist
./orion_scrobbler.py

# Scrobble a specific date
./orion_scrobbler.py --date 14.11.2025

# Preview what would be scrobbled (no Last.fm calls)
./orion_scrobbler.py --dry-run

# Preview a specific date
./orion_scrobbler.py --dry-run --date 14.11.2025

# Always pick the newest post, ignoring processed-state
./orion_scrobbler.py --latest

# List available playlist posts
./orion_scrobbler.py --list

# Debug: show raw scraped lines
./orion_scrobbler.py --dump-raw --date 14.11.2025

# Debug: show only track-candidate lines
./orion_scrobbler.py --dump-candidates --date 14.11.2025

# Verbose diagnostics
./orion_scrobbler.py --debug --dry-run
```

## State file

Processed dates are stored in `~/.orion_scrobbler_state.json` as `DD.MM.YYYY` (up to 80 entries). Delete or edit this file to re-scrobble a date.

## Authentication alternatives

If you don't use a session key, you can authenticate with username + password hash (not recommended):

```bash
export LASTFM_USER=your_username
export LASTFM_PASSWORD=your_password
```

## get_session_key.py options

| Flag | Description |
|---|---|
| `--open` | Open the auth URL in your browser automatically |
| `--poll` | Auto-poll until you approve in the browser |
| `--token TOKEN` | Provide the token manually from the URL bar |
| `--save PATH` | Save to a custom path (default: `~/.session_key`) |
