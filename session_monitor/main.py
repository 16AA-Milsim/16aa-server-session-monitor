import json
import os
import socket
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Dict, Iterable, Optional

import discord
from discord import app_commands
from discord.ext import tasks
from dotenv import load_dotenv

from .geo import GeoCache
from .state_store import StateStore
from .wevtutil_security import get_latest_rdp_logons
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
    last_rdp_geo: Optional[str]


class SessionMonitorClient(discord.Client):
    def __init__(self, *, intents: discord.Intents):
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

        self.guild_id = int(os.environ["GUILD_ID"])
        self.channel_id = int(os.environ["CHANNEL_ID"])
        self.admin_role_id = int(os.environ["ADMIN_ROLE_ID"]) if os.getenv("ADMIN_ROLE_ID") else None

        self.monitor_users = _parse_users(os.getenv("MONITOR_USERS", "16aa,cantina,16aa_public,16aa_testing"))
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

    async def setup_hook(self) -> None:
        guild = discord.Object(id=self.guild_id)
        self.tree.copy_global_to(guild=guild)
        await self.tree.sync(guild=guild)

    def _member_is_authorized(self, member: Optional[discord.Member]) -> bool:
        if member is None:
            return False
        if self.admin_role_id is None:
            return True
        return any(role.id == self.admin_role_id for role in getattr(member, "roles", []))

    async def on_ready(self) -> None:
        self._panel_message_id = self.state_store.get_panel_message_id()
        self.update_panel.start()
        print(f"Logged in as {self.user} (panel_message_id={self._panel_message_id})")

    async def _get_or_create_panel_message(self) -> discord.Message:
        channel = self.get_channel(self.channel_id)
        if channel is None or not isinstance(channel, discord.abc.Messageable):
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

    def _build_rows(
        self,
        sessions: Dict[str, SessionInfo],
        rdp_ip_by_user: Dict[str, tuple[Optional[str], Optional[datetime]]],
    ) -> list[UserPanelRow]:
        rows: list[UserPanelRow] = []
        for username in self.monitor_users:
            info = sessions.get(username)
            if info is None:
                idle = IdleInfo(raw="(none)", minutes=None)
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
                        last_rdp_geo=None,
                    )
                )
                continue

            engaged = info.state.lower() == "active" and info.idle.minutes is not None and info.idle.minutes <= self.idle_threshold_minutes
            last_ip, last_time = rdp_ip_by_user.get(username, (None, None))
            geo = self.geo_cache.get_geo_string(last_ip) if last_ip else None

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
                    last_rdp_geo=geo,
                )
            )
        return rows

    def _build_embed(self, *, rows: list[UserPanelRow], last_checked_utc: datetime) -> discord.Embed:
        embed = discord.Embed(
            title=f"RDP Session Monitor ({self.hostname})",
            color=discord.Color.blurple(),
            timestamp=last_checked_utc,
        )
        embed.set_footer(text=f"Idle threshold: {self.idle_threshold_minutes}m | Last checked")

        for row in rows:
            idle_display = row.idle.raw
            if row.idle.minutes is not None:
                idle_display = f"{row.idle.minutes}m ({row.idle.raw})"

            engaged_text = "Yes" if row.engaged else "No"

            session_bits = []
            if row.session_name:
                session_bits.append(row.session_name)
            if row.session_id:
                session_bits.append(f"ID {row.session_id}")
            session_display = " | ".join(session_bits) if session_bits else "(none)"

            lines = [
                f"State: `{row.state}` | Engaged: `{engaged_text}`",
                f"Idle: `{idle_display}`",
                f"Session: `{session_display}`",
            ]
            if row.logon_time_raw:
                lines.append(f"Logon: `{row.logon_time_raw}`")

            if row.last_rdp_ip:
                ip_line = f"Last RDP IP: `{row.last_rdp_ip}`"
                if row.last_rdp_geo:
                    ip_line += f" ({row.last_rdp_geo})"
                if row.last_rdp_time_utc:
                    ip_line += f" @ `{row.last_rdp_time_utc.astimezone(timezone.utc).strftime('%Y-%m-%d %H:%M:%SZ')}`"
                lines.append(ip_line)
            else:
                lines.append("Last RDP IP: `(unknown)`")

            embed.add_field(name=row.username, value="\n".join(lines), inline=False)

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

        rdp_ip_by_user: Dict[str, tuple[Optional[str], Optional[datetime]]] = {}
        if self._should_refresh_security():
            rdp_ip_by_user = get_latest_rdp_logons(self.monitor_users, max_events=250)
            self._last_security_poll_utc = now
            self._last_rdp_logons = rdp_ip_by_user
        else:
            rdp_ip_by_user = self._last_rdp_logons

        rows = self._build_rows(sessions, rdp_ip_by_user)
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

    missing = [k for k in ["DISCORD_TOKEN", "GUILD_ID", "CHANNEL_ID"] if not os.getenv(k)]
    if missing:
        print(f"Missing required env vars: {', '.join(missing)}", file=sys.stderr)
        sys.exit(2)

    intents = discord.Intents.none()
    client = SessionMonitorClient(intents=intents)

    @client.tree.command(name="status", description="Show current session status (ephemeral).")
    async def status_cmd(interaction: discord.Interaction) -> None:
        if not client._member_is_authorized(interaction.user if isinstance(interaction.user, discord.Member) else None):
            await interaction.response.send_message("Not authorized.", ephemeral=True)
            return
        sessions = get_quser_sessions()
        rdp = get_latest_rdp_logons(client.monitor_users, max_events=250)
        rows = client._build_rows(sessions, rdp)
        embed = client._build_embed(rows=rows, last_checked_utc=datetime.now(timezone.utc))
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @client.tree.command(name="refresh", description="Force refresh the panel now.")
    async def refresh_cmd(interaction: discord.Interaction) -> None:
        if not client._member_is_authorized(interaction.user if isinstance(interaction.user, discord.Member) else None):
            await interaction.response.send_message("Not authorized.", ephemeral=True)
            return
        await interaction.response.send_message("Refreshing…", ephemeral=True)
        client._last_embed_json = None
        await client.update_panel()

    client.run(os.environ["DISCORD_TOKEN"])
