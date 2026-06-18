"""Session persistence manager for airline auth tokens and cookies.

Stores both:
  - HTTP cookies (for Playwright browser reuse)
  - Bearer token (for direct API calls via httpx)

This avoids repeated login + MFA challenges.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

DEFAULT_SESSION_TTL_HOURS = 24
SESSION_VERSION = 1


class SessionManager:
    """Manages persistent auth sessions per airline.

    Each session file (~/.award_scout/sessions/{airline}_session.json) contains:
      - version: schema version
      - airline: airline identifier
      - cookies: list of browser cookies (Playwright-compatible format)
      - bearer_token: x-authorization-api token for direct API calls
      - created_at / expires_at: ISO8601 timestamps
      - metadata: arbitrary key-value (user info, etc.)
    """

    def __init__(self, session_dir: Path):
        self.session_dir = Path(session_dir)
        self.session_dir.mkdir(parents=True, exist_ok=True)

    # --- Path helpers ---

    def _file_path(self, airline: str) -> Path:
        return self.session_dir / f"{airline}_session.json"

    # --- Load ---

    def load(self, airline: str) -> dict[str, Any] | None:
        """Load a session. Returns None if missing or expired."""
        path = self._file_path(airline)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            return None

        if not isinstance(data, dict) or not data.get("bearer_token"):
            return None

        expires_str = data.get("expires_at", "")
        if self._is_expired(expires_str):
            path.unlink(missing_ok=True)
            return None

        return data

    def is_valid(self, airline: str) -> bool:
        return self.load(airline) is not None

    # --- Save ---

    def save(
        self,
        airline: str,
        cookies: list[dict[str, Any]],
        bearer_token: str,
        ttl_hours: int = DEFAULT_SESSION_TTL_HOURS,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        now = datetime.now(timezone.utc)
        data: dict[str, Any] = {
            "version": SESSION_VERSION,
            "airline": airline,
            "cookies": cookies,
            "bearer_token": bearer_token,
            "created_at": now.isoformat(),
            "expires_at": (now + timedelta(hours=ttl_hours)).isoformat(),
            "metadata": metadata or {},
        }
        path = self._file_path(airline)
        path.write_text(json.dumps(data, indent=2))

    # --- Delete ---

    def delete(self, airline: str) -> None:
        self._file_path(airline).unlink(missing_ok=True)

    def delete_all(self) -> None:
        for f in self.session_dir.glob("*_session.json"):
            f.unlink(missing_ok=True)

    # --- Accessors ---

    def get_bearer_token(self, airline: str) -> str | None:
        data = self.load(airline)
        if data:
            return data.get("bearer_token")
        return None

    def get_cookies_playwright(self, airline: str) -> list[dict[str, Any]] | None:
        """Return cookies in Playwright-compatible format."""
        data = self.load(airline)
        if data:
            return data.get("cookies")
        return None

    def get_cookies_httpx(self, airline: str) -> dict[str, str] | None:
        """Return cookies as {name: value} dict for httpx."""
        pw_cookies = self.get_cookies_playwright(airline)
        if pw_cookies is None:
            return None
        return {c["name"]: c["value"] for c in pw_cookies}

    # --- Internal ---

    @staticmethod
    def _is_expired(expires_str: str) -> bool:
        if not expires_str:
            return True
        try:
            expires = datetime.fromisoformat(expires_str)
            return datetime.now(timezone.utc) > expires
        except ValueError:
            return True


# ---------------------------------------------------------------------------
# Helper: extract x-authorization-api bearer token from a Playwright request
# ---------------------------------------------------------------------------

def extract_bearer_from_request_headers(
    headers: dict[str, str],
) -> str | None:
    """Extract the bearer token from request headers.

    United's API uses:  x-authorization-api: bearer DAAAA...
    """
    raw = headers.get("x-authorization-api", "")
    if raw.lower().startswith("bearer "):
        token = raw[7:].strip()
        if token:
            return token
    return None
