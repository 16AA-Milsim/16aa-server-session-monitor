import json
import os
import re
import socket
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Dict, Iterable, Optional

import discord
from discord.ext import tasks
from dotenv import load_dotenv

from .geo import GeoCache
from .state_store import StateStore
from .wevtutil_security import get_latest_rdp_disconnects, get_latest_rdp_logons
from .windows_sessions import IdleInfo, SessionInfo, get_quser_sessions


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    return int(value)


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def _parse_users(raw: str) -> list[str]:
    users = []
    for part in (raw or "").split(","):
        part = part.strip()
        if part:
            users.append(part.lower())
    return users


def _parse_aliases(raw: str) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for part in (raw or "").split(","):
        part = part.strip()
        if not part or "=" not in part:
            continue
        key, value = part.split("=", 1)
        key = key.strip().lower()
        value = value.strip()
        if key and value:
            aliases[key] = value
    return aliases


def _format_idle_minutes(minutes: int) -> str:
    if minutes < 60:
        return f"{minutes}m"
    hours, mins = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h {mins}m"
    days, hours = divmod(hours, 24)
    return f"{days}d {hours}h {mins}m"


def _format_logon_time(raw: str) -> str:
    if not raw:
        return raw
    match = re.search(r"(?i)(\d{1,2}):(\d{2})(?::\d{2})?\s*([ap]\.?m\.?)?", raw)
    if not match:
        return raw
    hour = int(match.group(1))
    minute = int(match.group(2))
    meridiem = match.group(3)
    if meridiem:
        meridiem = meridiem.lower().replace(".", "")
        if meridiem == "pm" and hour < 12:
            hour += 12
        elif meridiem == "am" and hour == 12:
            hour = 0
    formatted_time = f"{hour:02d}:{minute:02d}"
    start, end = match.span()
    return f"{raw[:start]}{formatted_time}{raw[end:]}"


def _format_event_time_local(dt: datetime) -> str:
    return dt.astimezone().strftime("%d/%m/%Y %H:%M")


def _format_duration_minutes(minutes: int) -> str:
    if minutes < 1:
        return "0m"
    hours, mins = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h {mins}m" if hours else f"{mins}m"
    days, hours = divmod(hours, 24)
    return f"{days}d {hours}h"


def _format_duration_since(dt: datetime, now: datetime) -> str:
    delta = now - dt
    if delta.total_seconds() < 0:
        delta = timedelta(seconds=0)
    minutes = int(delta.total_seconds() // 60)
    if minutes < 1:
        return "just now"
    return f"{_format_duration_minutes(minutes)} ago"


@dataclass(frozen=True)
class UserPanelRow:
    username: str
    state: str
    idle: IdleInfo
    engaged: bool
    session_id: Optional[str]
    session_name: Optional[str]
    logon_time_raw: Optional[str]
    last_rdp_ip: Optional[str]
    last_rdp_time_utc: Optional[datetime]
    last_rdp_disconnect_time_utc: Optional[datetime]
    pending_disconnect_since: Optional[datetime]
    pending_disconnect: bool
    last_rdp_geo: Optional[str]


class SessionMonitorClient(discord.Client):
    def __init__(self, *, intents: discord.Intents):
        super().__init__(intents=intents)
        self.channel_id = int(os.environ["CHANNEL_ID"])

        self.monitor_users = _parse_users(os.getenv("MONITOR_USERS", "16aa,cantina,16aa_public,16aa_testing"))
        self.user_aliases = _parse_aliases(os.getenv("USER_ALIASES", ""))
        self.idle_threshold_minutes = _env_int("IDLE_THRESHOLD_MINUTES", 10)
        self.poll_seconds = _env_int("POLL_SECONDS", 15)
        self.security_poll_seconds = _env_int("SECURITY_POLL_SECONDS", 60)

        self.hostname = socket.gethostname()
        self.state_store = StateStore("data/state.json")
        self.geo_cache = GeoCache("data/geo_cache.json", enabled=_env_bool("GEOLOOKUP_ENABLED", True))

        self._panel_message_id: Optional[int] = None
        self._last_embed_json: Optional[str] = None
        self._last_security_poll_utc: Optional[datetime] = None
        self._last_rdp_logons: Dict[str, tuple[Optional[str], Optional[datetime]]] = {
            u: (None, None) for u in self.monitor_users
        }
        self._last_rdp_disconnects: Dict[str, tuple[Optional[str], Optional[datetime]]] = {
            u: (None, None) for u in self.monitor_users
        }
        self._last_session_states: Dict[str, str] = {u: "" for u in self.monitor_users}
        self._pending_disconnect_since: Dict[str, Optional[datetime]] = {u: None for u in self.monitor_users}

    def _display_name(self, username: str) -> str:
        return self.user_aliases.get(username.lower(), username)

    def _status_dot(self, row: UserPanelRow) -> str:
        if row.state.lower() == "active":
            return ":red_circle:" if row.engaged else ":yellow_circle:"
        return ":green_circle:"

    def _pending_disconnect_tolerance(self) -> timedelta:
        return timedelta(seconds=max(self.poll_seconds, self.security_poll_seconds) * 2)

    async def on_ready(self) -> None:
        self._panel_message_id = self.state_store.get_panel_message_id()
        self.update_panel.start()
        print(f"Logged in as {self.user} (panel_message_id={self._panel_message_id})")

    async def close(self) -> None:
        await self._delete_panel_message()
        await super().close()

    async def _get_or_create_panel_message(self) -> discord.Message:
        channel = self.get_channel(self.channel_id)
        if channel is None:
            try:
                channel = await self.fetch_channel(self.channel_id)
            except discord.NotFound as exc:
                raise RuntimeError("Configured CHANNEL_ID does not exist or bot cannot see it.") from exc
            except discord.Forbidden as exc:
                raise RuntimeError("Bot lacks permissions to access the configured CHANNEL_ID.") from exc
            except discord.HTTPException as exc:
                raise RuntimeError("Failed to fetch configured CHANNEL_ID from Discord.") from exc

        if not isinstance(channel, discord.abc.Messageable):
            raise RuntimeError("Configured CHANNEL_ID is not a messageable channel or bot cannot see it.")

        if self._panel_message_id is not None:
            try:
                return await channel.fetch_message(self._panel_message_id)
            except discord.NotFound:
                self._panel_message_id = None
                self.state_store.set_panel_message_id(None)

        message = await channel.send(embed=self._build_embed(rows=[], last_checked_utc=datetime.now(timezone.utc)))
        self._panel_message_id = message.id
        self.state_store.set_panel_message_id(message.id)
        return message

    async def _delete_panel_message(self) -> None:
        if self._panel_message_id is None:
            return

        channel = self.get_channel(self.channel_id)
        if channel is None:
            try:
                channel = await self.fetch_channel(self.channel_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                return

        if not isinstance(channel, discord.abc.Messageable):
            return

        deleted = False
        try:
            message = await channel.fetch_message(self._panel_message_id)
        except discord.NotFound:
            deleted = True
        except (discord.Forbidden, discord.HTTPException):
            return
        else:
            try:
                await message.delete()
                deleted = True
            except (discord.Forbidden, discord.HTTPException):
                return

        if deleted:
            self._panel_message_id = None
            self.state_store.set_panel_message_id(None)

    def _build_rows(
        self,
        sessions: Dict[str, SessionInfo],
        rdp_ip_by_user: Dict[str, tuple[Optional[str], Optional[datetime]]],
        rdp_disconnect_by_user: Dict[str, tuple[Optional[str], Optional[datetime]]],
    ) -> list[UserPanelRow]:
        rows: list[UserPanelRow] = []
        for username in self.monitor_users:
            info = sessions.get(username)
            if info is None:
                idle = IdleInfo(raw="(none)", minutes=None)
                pending_since = self._pending_disconnect_since.get(username)
                pending_disconnect = pending_since is not None
                rows.append(
                    UserPanelRow(
                        username=username,
                        state="Missing",
                        idle=idle,
                        engaged=False,
                        session_id=None,
                        session_name=None,
                        logon_time_raw=None,
                        last_rdp_ip=rdp_ip_by_user.get(username, (None, None))[0],
                        last_rdp_time_utc=rdp_ip_by_user.get(username, (None, None))[1],
                        last_rdp_disconnect_time_utc=rdp_disconnect_by_user.get(username, (None, None))[1],
                        pending_disconnect_since=pending_since,
                        pending_disconnect=pending_disconnect,
                        last_rdp_geo=None,
                    )
                )
                continue

            engaged = info.state.lower() == "active" and info.idle.minutes is not None and info.idle.minutes <= self.idle_threshold_minutes
            last_ip, last_time = rdp_ip_by_user.get(username, (None, None))
            _, last_disconnect_time = rdp_disconnect_by_user.get(username, (None, None))
            geo = self.geo_cache.get_geo_string(last_ip) if last_ip else None
            pending_since = self._pending_disconnect_since.get(username)
            pending_disconnect = pending_since is not None

            rows.append(
                UserPanelRow(
                    username=username,
                    state=info.state,
                    idle=info.idle,
                    engaged=engaged,
                    session_id=info.session_id,
                    session_name=info.session_name,
                    logon_time_raw=info.logon_time_raw,
                    last_rdp_ip=last_ip,
                    last_rdp_time_utc=last_time,
                    last_rdp_disconnect_time_utc=last_disconnect_time,
                    pending_disconnect_since=pending_since,
                    pending_disconnect=pending_disconnect,
                    last_rdp_geo=geo,
                )
            )
        return rows

    def _build_embed(self, *, rows: list[UserPanelRow], last_checked_utc: datetime) -> discord.Embed:
        embed = discord.Embed(
            title=f"RDP Session Monitor ({self.hostname})",
            color=discord.Color.from_rgb(70, 70, 70),
            timestamp=last_checked_utc,
        )
        embed.set_footer(text="Last checked")

        for row in rows:
            idle_display = row.idle.raw
            if row.idle.minutes is not None:
                idle_display = _format_idle_minutes(row.idle.minutes)

            state_display = "Disconnected" if row.state.lower() == "disc" else row.state
            lines = []
            if row.state.lower() == "active":
                engaged_text = "Yes" if row.engaged else "No"
                lines.append(f"State: `{state_display}` | Engaged: `{engaged_text}` | Idle: `{idle_display}`")
                minutes = None
                if row.last_rdp_time_utc:
                    minutes = int(max(0, (last_checked_utc - row.last_rdp_time_utc).total_seconds()) // 60)
                duration = _format_duration_minutes(minutes or 0) if minutes is not None else None

                if row.last_rdp_ip:
                    ip_line = f"Connected: `{row.last_rdp_ip}`"
                    if row.last_rdp_geo:
                        ip_line += f" ({row.last_rdp_geo})"
                    if duration:
                        ip_line += f" | `{duration}`"
                    lines.append(ip_line)
                else:
                    ip_line = "Connected: `(unknown)`"
                    if duration:
                        ip_line += f" | `{duration}`"
                    lines.append(ip_line)
            else:
                lines.append(f"State: `{state_display}`")
                if row.pending_disconnect_since:
                    tolerance = self._pending_disconnect_tolerance()
                    if row.last_rdp_disconnect_time_utc is None or row.last_rdp_disconnect_time_utc < (row.pending_disconnect_since - tolerance):
                        lines.append("Last Connected: `...`")
                        field_name = f"{self._status_dot(row)} {self._display_name(row.username)}"
                        embed.add_field(name=field_name, value="\n".join(lines), inline=False)
                        continue

                if row.pending_disconnect and row.last_rdp_disconnect_time_utc is None:
                    lines.append("Last Connected: `...`")
                elif row.last_rdp_disconnect_time_utc:
                    last_connected_display = _format_event_time_local(row.last_rdp_disconnect_time_utc)
                    duration = _format_duration_since(row.last_rdp_disconnect_time_utc, last_checked_utc)
                    lines.append(f"Last Connected: `{last_connected_display} ({duration})`")
                elif row.last_rdp_time_utc:
                    last_connected_display = _format_event_time_local(row.last_rdp_time_utc)
                    duration = _format_duration_since(row.last_rdp_time_utc, last_checked_utc)
                    lines.append(f"Last Connected: `{last_connected_display} ({duration})`")
                elif row.logon_time_raw:
                    last_connected_display = _format_logon_time(row.logon_time_raw)
                    lines.append(f"Last Connected: `{last_connected_display}`")
                else:
                    lines.append("Last Connected: `-`")

            field_name = f"{self._status_dot(row)} {self._display_name(row.username)}"
            embed.add_field(name=field_name, value="\n".join(lines), inline=False)

        return embed

    def _embed_to_stable_json(self, embed: discord.Embed) -> str:
        payload = embed.to_dict()
        payload.pop("timestamp", None)
        return json.dumps(payload, sort_keys=True, separators=(",", ":"))

    def _should_refresh_security(self) -> bool:
        now = datetime.now(timezone.utc)
        if self._last_security_poll_utc is None:
            return True
        return (now - self._last_security_poll_utc) >= timedelta(seconds=self.security_poll_seconds)

    @tasks.loop(seconds=15)
    async def update_panel(self) -> None:
        now = datetime.now(timezone.utc)
        sessions = get_quser_sessions()

        current_states: Dict[str, str] = {}
        for username in self.monitor_users:
            info = sessions.get(username)
            current_states[username] = info.state.lower() if info is not None else "missing"

        for username, current_state in current_states.items():
            prev_state = self._last_session_states.get(username, "")
            if prev_state == "active" and current_state != "active":
                self._pending_disconnect_since[username] = now
            elif current_state == "active":
                self._pending_disconnect_since[username] = None
            self._last_session_states[username] = current_state

        rdp_ip_by_user: Dict[str, tuple[Optional[str], Optional[datetime]]] = {}
        rdp_disconnect_by_user: Dict[str, tuple[Optional[str], Optional[datetime]]] = {}
        if self._should_refresh_security():
            rdp_ip_by_user = get_latest_rdp_logons(self.monitor_users, max_events=250)
            rdp_disconnect_by_user = get_latest_rdp_disconnects(self.monitor_users, max_events=250)
            self._last_security_poll_utc = now
            self._last_rdp_logons = rdp_ip_by_user
            self._last_rdp_disconnects = rdp_disconnect_by_user
            tolerance = self._pending_disconnect_tolerance()
            for username, pending_since in self._pending_disconnect_since.items():
                if pending_since is None:
                    continue
                disconnect_time = rdp_disconnect_by_user.get(username, (None, None))[1]
                if disconnect_time and disconnect_time >= (pending_since - tolerance):
                    self._pending_disconnect_since[username] = None
        else:
            rdp_ip_by_user = self._last_rdp_logons
            rdp_disconnect_by_user = self._last_rdp_disconnects

        rows = self._build_rows(sessions, rdp_ip_by_user, rdp_disconnect_by_user)
        embed = self._build_embed(rows=rows, last_checked_utc=now)

        message = await self._get_or_create_panel_message()

        embed_json = self._embed_to_stable_json(embed)
        if embed_json != self._last_embed_json:
            await message.edit(embed=embed)
            self._last_embed_json = embed_json

    @update_panel.before_loop
    async def _before_update_panel(self) -> None:
        self.update_panel.change_interval(seconds=self.poll_seconds)
        await self.wait_until_ready()


def main() -> None:
    load_dotenv()

    missing = [k for k in ["DISCORD_TOKEN", "CHANNEL_ID"] if not os.getenv(k)]
    if missing:
        print(f"Missing required env vars: {', '.join(missing)}", file=sys.stderr)
        sys.exit(2)

    intents = discord.Intents.none()
    client = SessionMonitorClient(intents=intents)

    client.run(os.environ["DISCORD_TOKEN"])
