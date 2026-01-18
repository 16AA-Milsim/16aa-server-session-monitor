import subprocess
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import Dict, Iterable, Optional


def _parse_event_time_utc(value: str) -> Optional[datetime]:
    # Example: 2026-01-18T12:34:56.1234567Z
    if not value:
        return None
    value = value.strip()
    try:
        if value.endswith("Z"):
            value = value[:-1]
            # Trim to microseconds (6) if present
            if "." in value:
                left, frac = value.split(".", 1)
                frac = (frac + "000000")[:6]
                value = f"{left}.{frac}"
                dt = datetime.fromisoformat(value)
            else:
                dt = datetime.fromisoformat(value)
            return dt.replace(tzinfo=timezone.utc)
        return datetime.fromisoformat(value).astimezone(timezone.utc)
    except Exception:
        return None


def _event_data_map(event: ET.Element) -> dict:
    ns = {"e": "http://schemas.microsoft.com/win/2004/08/events/event"}
    data = {}
    for data_node in event.findall(".//e:EventData/e:Data", ns):
        name = data_node.attrib.get("Name")
        if not name:
            continue
        data[name] = (data_node.text or "").strip()
    return data


def get_latest_rdp_logons(
    usernames: Iterable[str],
    *,
    max_events: int = 250,
) -> Dict[str, tuple[Optional[str], Optional[datetime]]]:
    """
    Best-effort: returns username -> (ip, time_utc) for latest RDP logon.
    """
    allowed_logon_types = {"10", "7"}
    wanted = {u.lower() for u in usernames}
    result: Dict[str, tuple[Optional[str], Optional[datetime]]] = {u: (None, None) for u in wanted}

    cp = subprocess.run(
        [
            "wevtutil",
            "qe",
            "Security",
            "/q:*[System[(EventID=4624)]] and *[EventData[Data[@Name='LogonType']='10' or Data[@Name='LogonType']='7']]",
            "/f:xml",
            "/rd:true",
            f"/c:{int(max_events)}",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    raw = (cp.stdout or "").strip()
    if not raw:
        return result

    # wevtutil emits multiple <Event>...</Event> blocks without a single root.
    xml = f"<Events>{raw}</Events>"
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return result

    ns = {"e": "http://schemas.microsoft.com/win/2004/08/events/event"}

    for event in root.findall("e:Event", ns):
        system = event.find("e:System", ns)
        if system is None:
            continue

        time_created = system.find("e:TimeCreated", ns)
        event_time_utc = _parse_event_time_utc(time_created.attrib.get("SystemTime", "") if time_created is not None else "")

        data = _event_data_map(event)
        target_user = (data.get("TargetUserName") or "").lower()
        if target_user not in wanted:
            continue

        logon_type = data.get("LogonType")
        if logon_type not in allowed_logon_types:
            continue

        ip = data.get("IpAddress") or ""
        if not ip or ip in {"-", "::1", "127.0.0.1"}:
            continue

        current_ip, current_time = result.get(target_user, (None, None))
        if current_time is None or (event_time_utc is not None and event_time_utc > current_time):
            result[target_user] = (ip, event_time_utc)

    return result

