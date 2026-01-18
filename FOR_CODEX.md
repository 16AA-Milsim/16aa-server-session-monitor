# FOR_CODEX.md

This repo contains a Discord bot that monitors RDP session activity for specific Windows accounts on a Windows Server (Server 2022 in our case) and posts/updates a single “panel” message in a Discord admin channel.

## What we’re building / why

Our milsim admins share Windows accounts on a game server box (Arma 3, etc.). If someone RDPs into the same account while another admin is using it, the first admin can get kicked off. The goal is to show, in Discord, whether each shared account is currently being actively used via RDP so admins can avoid connecting and disrupting each other.

We monitor these accounts (Windows usernames):
- `16AA` → `16aa`
- `16AA Admin` → `cantina`
- `16AA Public` → `16aa_public`
- `16AA Testing` → `16aa_testing`

Important: the bot does **not** need Windows account passwords. It reads session status locally from Windows (`quser`) + Security Event Logs.

## “Occupied/Engaged” definition

Per account:
- Engaged = `quser` shows `STATE=Active` **and** `IDLE <= 10 minutes`.
- If `STATE=Active` but idle is over threshold, show Engaged = `No`.
- `Disc` means disconnected (not engaged).

The Discord panel is **per-account rows only** (no overall server status).

## How it works (data sources)

### Live session state (authoritative for “active now”)
- Uses `quser` output to get:
  - state (`Active` / `Disc`)
  - idle time (including formats like `.`, `21`, `5+23:43`, `86+15:31`)
  - session id/name (when present)
  - logon time (best-effort display; parsing varies by locale)

Implementation: `session_monitor/windows_sessions.py`

### Client IP + enrichment (best-effort)
- Reads Windows Security Event Log via:
  - Event ID `4624`
  - `LogonType = 10` (RDP / RemoteInteractive)
  - `TargetUserName` matches monitored usernames
  - uses `IpAddress` from event XML

Implementation: `session_monitor/wevtutil_security.py`

### Optional geolocation
- If enabled, calls `https://ipapi.co/<ip>/json/`
- Results cached in `data/geo_cache.json` for 24 hours

Implementation: `session_monitor/geo.py`

## Discord behavior

- Single combined panel message in a configured channel.
- The bot edits that message in place (no spam).
- Polls `quser` every `POLL_SECONDS` (default 15).
- Refreshes Security log enrichment every `SECURITY_POLL_SECONDS` (default 60).

Slash commands (guild-scoped):
- `/status` → returns the current embed (ephemeral)
- `/refresh` → forces a panel refresh now (ephemeral)

Role-gating:
- If `ADMIN_ROLE_ID` is set, slash commands require that role.
- The panel itself updates regardless (channel should be admin-only).

Implementation: `session_monitor/main.py`

## Config

Create `.env` (see `.env.example`):
- `DISCORD_TOKEN` (required)
- `GUILD_ID` (required)
- `CHANNEL_ID` (required)
- `ADMIN_ROLE_ID` (optional)
- `MONITOR_USERS` (default `16aa,cantina,16aa_public,16aa_testing`)
- `IDLE_THRESHOLD_MINUTES` (default `10`)
- `POLL_SECONDS` (default `15`)
- `SECURITY_POLL_SECONDS` (default `60`)
- `GEOLOOKUP_ENABLED` (default `true`)

State:
- The bot stores the panel message id in `data/state.json` so it reuses the same message after restarts.

## Run locally (quick)

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m session_monitor
```

If env vars are missing, the bot exits with: “Missing required env vars…”.

## Deployment (recommended on Windows Server)

Run the bot on the Windows server itself (best access to `quser` + Security logs):
- Create a Scheduled Task:
  - Trigger: At startup
  - Action: run `python -m session_monitor` (in repo folder with venv activated or using full venv python path)
  - Restart on failure
  - Run whether user is logged on or not (optional; depends on your preference)

Windows permissions:
- `quser` usually works.
- Reading Security log for 4624 may require:
  - run as admin, and/or
  - add the bot’s Windows account to `Event Log Readers`.

## Repo pointers

- Main bot: `session_monitor/main.py`
- `quser` parsing: `session_monitor/windows_sessions.py`
- Security log (IP): `session_monitor/wevtutil_security.py`
- Geo cache: `session_monitor/geo.py`
- Message id persistence: `session_monitor/state_store.py`
- Setup instructions: `README.md`

## Known limitations / gotchas

- `quser` logon time format is locale-dependent; we display it as-is.
- Security log IP is “best-effort” and may be blank/missing; panel shows `(unknown)` then.
- Geo lookup may be inaccurate for VPN/LAN IPs; we still attempt it when an IP exists.

