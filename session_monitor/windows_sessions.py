import re
import subprocess
from dataclasses import dataclass
from typing import Dict, Optional


@dataclass(frozen=True)
class IdleInfo:
    raw: str
    minutes: Optional[int]


@dataclass(frozen=True)
class SessionInfo:
    username: str
    session_name: Optional[str]
    session_id: Optional[str]
    state: str
    idle: IdleInfo
    logon_time_raw: Optional[str]


def _parse_idle_to_minutes(raw: str) -> Optional[int]:
    raw = (raw or "").strip()
    if raw in {".", "none", "None", ""}:
        return 0

    m = re.fullmatch(r"(\d+)\+(\d+):(\d+)", raw)
    if m:
        days = int(m.group(1))
        hours = int(m.group(2))
        minutes = int(m.group(3))
        return (days * 24 + hours) * 60 + minutes

    m = re.fullmatch(r"(\d+):(\d+)", raw)
    if m:
        hours = int(m.group(1))
        minutes = int(m.group(2))
        return hours * 60 + minutes

    if raw.isdigit():
        return int(raw)

    return None


def get_quser_sessions() -> Dict[str, SessionInfo]:
    """
    Returns a map of lowercase username -> SessionInfo from `quser`.
    """
    try:
        cp = subprocess.run(
            ["quser"],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        return {}

    text = (cp.stdout or "").strip()
    if not text:
        return {}

    lines = [ln.rstrip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        return {}

    sessions: Dict[str, SessionInfo] = {}
    for line in lines[1:]:
        line = line.lstrip(">").rstrip()
        parts = re.split(r"\s{2,}", line.strip())
        if len(parts) < 5:
            continue

        username = parts[0].strip().lower()

        # If SESSIONNAME is missing, the second column becomes ID (digit)
        session_name: Optional[str]
        session_id: Optional[str]
        state: str
        idle_raw: str
        logon_raw: str

        if len(parts) >= 5 and parts[1].isdigit():
            session_name = None
            session_id = parts[1]
            state = parts[2]
            idle_raw = parts[3]
            logon_raw = " ".join(parts[4:]).strip() or None
        else:
            session_name = parts[1] if parts[1] else None
            session_id = parts[2] if len(parts) > 2 else None
            state = parts[3] if len(parts) > 3 else "Unknown"
            idle_raw = parts[4] if len(parts) > 4 else ""
            logon_raw = " ".join(parts[5:]).strip() if len(parts) > 5 else None

        sessions[username] = SessionInfo(
            username=username,
            session_name=session_name,
            session_id=session_id,
            state=state,
            idle=IdleInfo(raw=idle_raw, minutes=_parse_idle_to_minutes(idle_raw)),
            logon_time_raw=logon_raw,
        )

    return sessions

