"""Configuration management for award_scout."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- United MileagePlus ---
    united_mp_number: Optional[str] = None
    united_password: Optional[str] = None

    # --- American AAdvantage ---
    aa_number: Optional[str] = None
    aa_password: Optional[str] = None

    # --- Delta SkyMiles ---
    delta_number: Optional[str] = None
    delta_password: Optional[str] = None

    # --- Storage ---
    data_dir: str = "~/.award_scout"

    # --- Browser ---
    headless: bool = True
    browser_timeout_ms: int = 60_000

    # --- Monitoring ---
    watch_interval_minutes: int = 120

    # --- Notifications: ntfy.sh ---
    ntfy_topic: Optional[str] = None
    ntfy_server: str = "https://ntfy.sh"

    # --- Notifications: Email (SMTP) ---
    email_to: Optional[str] = None
    email_from: Optional[str] = None
    email_smtp_host: str = "smtp.163.com"
    email_smtp_port: int = 465
    email_smtp_user: Optional[str] = None
    email_smtp_password: Optional[str] = None

    @property
    def data_path(self) -> Path:
        p = Path(self.data_dir).expanduser()
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def db_path(self) -> Path:
        return self.data_path / "awards.db"

    @property
    def cookies_dir(self) -> Path:
        d = self.data_path / "cookies"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def cookie_path(self, airline: str) -> Path:
        return self.cookies_dir / f"{airline}_cookies.json"

    @property
    def sessions_dir(self) -> Path:
        d = self.data_path / "sessions"
        d.mkdir(parents=True, exist_ok=True)
        return d


settings = Settings()
