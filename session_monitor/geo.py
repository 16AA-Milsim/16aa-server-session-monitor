import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

import requests


@dataclass
class GeoResult:
    summary: str
    fetched_at_utc: datetime


class GeoCache:
    def __init__(
        self,
        path: str,
        *,
        enabled: bool = True,
        ttl_hours: int = 24,
        failure_ttl_minutes: int = 30,
        error_log_interval_minutes: int = 10,
    ):
        self.path = path
        self.enabled = enabled
        self.ttl = timedelta(hours=ttl_hours)
        self.failure_ttl = timedelta(minutes=failure_ttl_minutes)
        self._error_log_interval = timedelta(minutes=error_log_interval_minutes)
        self._last_error_utc: Optional[datetime] = None
        self._last_error_msg: Optional[str] = None
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
                summary = cached.get("summary")
                fetched_raw = cached.get("fetched_at_utc")
                if summary and fetched_raw:
                    fetched = datetime.fromisoformat(fetched_raw)
                    if fetched.tzinfo is None:
                        fetched = fetched.replace(tzinfo=timezone.utc)
                    if (now - fetched) <= self.ttl:
                        return summary
                failed_raw = cached.get("failed_at_utc")
                if failed_raw:
                    failed = datetime.fromisoformat(failed_raw)
                    if failed.tzinfo is None:
                        failed = failed.replace(tzinfo=timezone.utc)
                    if (now - failed) <= self.failure_ttl:
                        return None
            except Exception:
                pass

        summary, error = self._lookup_ipwho_is(ip)
        if summary:
            self._cache[ip] = {"summary": summary, "fetched_at_utc": now.isoformat()}
            self._write()
        elif error:
            self._cache[ip] = {"summary": None, "failed_at_utc": now.isoformat(), "failure_reason": error}
            self._write()
            self._log_error(ip, error)
        return summary

    def _lookup_ipwho_is(self, ip: str) -> tuple[Optional[str], Optional[str]]:
        try:
            resp = requests.get(
                f"https://ipwho.is/{ip}",
                params={"fields": "success,message,city,region,country,org,connection.isp,connection.org"},
                timeout=6,
            )
            if resp.status_code != 200:
                return None, f"ipwho status {resp.status_code}"
            data = resp.json()
            if not data.get("success", True):
                reason = data.get("message") or "ipwho error"
                return None, reason
            city = data.get("city")
            region = data.get("region")
            country = data.get("country")
            org = data.get("org")
            if not org:
                connection = data.get("connection") or {}
                org = connection.get("org") or connection.get("isp")

            parts = [p for p in [city, region, country] if p]
            loc = ", ".join(parts) if parts else None
            if loc and org:
                return f"{loc} | {org}", None
            return (loc or org), None
        except Exception as exc:
            return None, f"ipwho request failed ({exc.__class__.__name__})"

    def _log_error(self, ip: str, message: str) -> None:
        now = datetime.now(timezone.utc)
        if self._last_error_msg == message and self._last_error_utc:
            if (now - self._last_error_utc) <= self._error_log_interval:
                return
        self._last_error_msg = message
        self._last_error_utc = now
        print(f"[geo] lookup failed for {ip}: {message}", file=sys.stderr)

