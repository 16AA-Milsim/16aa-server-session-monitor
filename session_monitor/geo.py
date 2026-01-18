import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

import requests


@dataclass
class GeoResult:
    summary: str
    fetched_at_utc: datetime


class GeoCache:
    def __init__(self, path: str, *, enabled: bool = True, ttl_hours: int = 24):
        self.path = path
        self.enabled = enabled
        self.ttl = timedelta(hours=ttl_hours)
        self._cache = self._read()

    def _read(self) -> dict:
        if not os.path.exists(self.path):
            return {}
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def _write(self) -> None:
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self._cache, f, indent=2, sort_keys=True)
        os.replace(tmp, self.path)

    def get_geo_string(self, ip: str) -> Optional[str]:
        if not self.enabled:
            return None
        ip = (ip or "").strip()
        if not ip:
            return None

        now = datetime.now(timezone.utc)
        cached = self._cache.get(ip)
        if cached:
            try:
                fetched = datetime.fromisoformat(cached["fetched_at_utc"])
                if fetched.tzinfo is None:
                    fetched = fetched.replace(tzinfo=timezone.utc)
                if (now - fetched) <= self.ttl:
                    return cached.get("summary")
            except Exception:
                pass

        summary = self._lookup_ipapi_co(ip)
        if summary:
            self._cache[ip] = {"summary": summary, "fetched_at_utc": now.isoformat()}
            self._write()
        return summary

    def _lookup_ipapi_co(self, ip: str) -> Optional[str]:
        try:
            resp = requests.get(f"https://ipapi.co/{ip}/json/", timeout=6)
            if resp.status_code != 200:
                return None
            data = resp.json()
            if data.get("error"):
                return None
            city = data.get("city")
            region = data.get("region")
            country = data.get("country_name") or data.get("country")
            org = data.get("org")

            parts = [p for p in [city, region, country] if p]
            loc = ", ".join(parts) if parts else None
            if loc and org:
                return f"{loc} | {org}"
            return loc or org
        except Exception:
            return None

