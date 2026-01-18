# 16AA Server Session Monitor (Discord Bot)

Discord bot that monitors active RDP sessions on a Windows Server using `quser`, enriches with the last RDP client IP from Security Event Log (4624 / LogonType 10 or 7), and updates a single "panel" message in Discord.

## Features

- Monitors multiple Windows usernames (default: `16aa`, `cantina`, `16aa_public`, `16aa_testing`)
- “Engaged” detection: `STATE=Active` and `IDLE <= IDLE_THRESHOLD_MINUTES` (default 10)
- Shows session state, idle time, logon time (from `quser`)
- Shows last RDP client IP (from Security event log) and optional geo lookup
- One combined panel message, edited in place (no channel spam)

## Setup

### 1) Create a Discord application + bot

- In Discord Developer Portal: create an application → add a bot → copy token
- Invite bot to your server with scopes:
  - `bot`
- Bot permissions needed in the target channel:
  - View Channel, Send Messages, Embed Links, Read Message History

### 2) Configure environment

Copy `.env.example` to `.env` and fill in values.

### 3) Install Python dependencies

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

### 4) Run

```powershell
.\.venv\Scripts\Activate.ps1
python -m session_monitor
```

On first run, the bot posts the panel message and stores its message id in `data/state.json`. Subsequent runs will re-use it.

## NSSM log rotation

If you run this bot under NSSM with stdout/stderr redirected to files, set rotation so the logs do not grow forever:

```powershell
.\nssm.exe set 16aa-server-session-monitor AppRotateBytes 5242880
.\nssm.exe set 16aa-server-session-monitor AppRotateFiles 5
.\nssm.exe set 16aa-server-session-monitor AppRotateOnline 1
```

## Windows permissions

- `quser` generally works for local users.
- Reading the Security log for Event ID 4624 may require:
  - running as admin, and/or
  - adding the bot’s Windows account to `Event Log Readers`.

## Geolocation

By default, the bot can do a best-effort lookup using `https://ipapi.co/<ip>/json/`. This is optional and cached in `data/geo_cache.json`.

Disable with `GEOLOOKUP_ENABLED=false`.

