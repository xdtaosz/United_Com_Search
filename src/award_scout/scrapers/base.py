from __future__ import annotations

import asyncio
import json
from abc import ABC, abstractmethod
from pathlib import Path
from collections.abc import Callable
from typing import Any, Optional

from playwright.async_api import BrowserContext, Page, Playwright, async_playwright

from award_scout.config import settings
from award_scout.models import AwardOffer, SearchQuery


class ScraperError(Exception):
    pass


class LoginError(ScraperError):
    pass


class MFARequired(ScraperError):
    def __init__(self, message: str = "MFA code required"):
        self.message: str = message
        super().__init__(message)


def _find_chrome() -> str | None:
    import shutil

    for path in (
        "/usr/bin/google-chrome-stable",
        "/usr/bin/google-chrome",
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
        "/snap/bin/chromium",
    ):
        if Path(path).is_file():
            return path

    candidates = sorted(Path.home().glob(".cache/ms-playwright/chromium-*/chrome-linux*/chrome"))
    if candidates:
        return str(candidates[0])

    system = shutil.which("google-chrome-stable") or shutil.which("google-chrome") or shutil.which("chromium")
    return system


class BaseAirlineScraper(ABC):
    """Base class for airline award scraper implementations.

    Each airline scraper handles:
      1. Browser login (with cookie caching)
      2. MFA challenge (delegated to user via callback)
      3. Award search via browser API interception or DOM parsing
      4. Parsing results into AwardOffer objects
    """

    def __init__(self, airline_name: str):
        self.airline_name: str = airline_name
        self._playwright: Playwright | None = None
        self._context: BrowserContext | None = None
        self._mfa_callback: Callable[[], Any] | None = None

    @property
    @abstractmethod
    def login_url(self) -> str:
        ...

    @property
    def cookie_file(self) -> Path:
        return settings.cookie_path(self.airline_name)

    def set_mfa_callback(self, callback: Callable[[], Any]) -> None:
        self._mfa_callback = callback

    async def _ensure_browser(self) -> BrowserContext:
        if self._context and not self._context.is_closed():
            return self._context
        self._playwright = await async_playwright().start()
        browser = await self._playwright.chromium.launch(
            headless=settings.headless,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
            ],
            executable_path=_find_chrome(),
        )
        self._context = await browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            locale="en-US",
            timezone_id="America/New_York",
        )
        return self._context

    async def load_cookies(self) -> bool:
        """Load saved cookies. Returns True if cookies were loaded."""
        if not self.cookie_file.exists():
            return False
        ctx = await self._ensure_browser()
        cookies = json.loads(self.cookie_file.read_text())
        await ctx.add_cookies(cookies)
        return True

    async def save_cookies(self) -> None:
        ctx = await self._ensure_browser()
        cookies = await ctx.cookies()
        self.cookie_file.write_text(json.dumps(cookies, indent=2))

    async def new_page(self) -> Page:
        ctx = await self._ensure_browser()
        page = await ctx.new_page()
        page.set_default_timeout(settings.browser_timeout_ms)
        return page

    async def close(self) -> None:
        if self._context:
            await self._context.close()
            self._context = None
        if self._playwright:
            await self._playwright.stop()
            self._playwright = None

    # --- Login flow ---

    async def login(self) -> bool:
        """Full login flow with cookie cache + MFA handling."""
        if await self.load_cookies():
            ctx = await self._ensure_browser()
            page = await ctx.new_page()
            try:
                await page.goto(self.login_url, wait_until="domcontentloaded")
                await asyncio.sleep(2)
                if await self._is_logged_in(page):
                    await page.close()
                    return True
            except Exception:
                pass
            await page.close()

        return await self._do_login()

    @abstractmethod
    async def _is_logged_in(self, page: Page) -> bool:
        """Check if the current page shows a logged-in state."""
        ...

    @abstractmethod
    async def _do_login(self) -> bool:
        """Perform fresh login (enter credentials, handle MFA)."""
        ...

    # --- Search ---

    @abstractmethod
    async def search(self, query: SearchQuery) -> list[AwardOffer]:
        """Search award availability and return parsed offers."""
        ...

    async def __aenter__(self) -> "BaseAirlineScraper":
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.close()
