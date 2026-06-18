from __future__ import annotations

import asyncio
import json
from abc import ABC, abstractmethod
from pathlib import Path
from collections.abc import Callable
from typing import Any, Optional

import httpx
from playwright.async_api import BrowserContext, Page, Playwright, async_playwright

from award_scout.config import settings
from award_scout.models import AwardOffer, SearchQuery
from award_scout.session_manager import SessionManager, extract_bearer_from_request_headers


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
        self._bearer_token: str | None = None
        self._session: SessionManager = SessionManager(settings.sessions_dir)

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

    @property
    def bearer_token(self) -> str | None:
        """Current bearer token, if authenticated."""
        return self._bearer_token

    async def login(self) -> bool:
        """Full login flow: session → cookies → fresh login.

        Priority:
          1. Load saved bearer token + validate via API probe
          2. Load saved cookies + check browser login state
          3. Full login with credentials + MFA
        """
        # 1. Try saved session
        saved = self._session.load(self.airline_name)
        if saved:
            token = saved.get("bearer_token")
            if token and await self._validate_token(token):
                self._bearer_token = token
                self._touch_session()
                return True

        # 2. Try cookie-based login
        if await self.load_cookies():
            ctx = await self._ensure_browser()
            page = await ctx.new_page()
            try:
                await page.goto(self.login_url, wait_until="domcontentloaded")
                await asyncio.sleep(2)
                if await self._is_logged_in(page):
                    await page.close()
                    self._bearer_token = await self._capture_bearer_token(ctx)
                    if self._bearer_token:
                        await self._save_full_session()
                    return True
            except Exception:
                pass
            await page.close()

        # 3. Full login
        return await self._do_login()

    async def _validate_token(self, token: str) -> bool:
        """Check if a bearer token is still valid via a lightweight API probe."""
        validation_url = self._token_validation_url()
        if not validation_url:
            return False
        try:
            cookies = self._session.get_cookies_httpx(self.airline_name) or {}
            headers = {
                "Content-Type": "application/json",
                "x-authorization-api": f"bearer {token}",
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                ),
            }
            async with httpx.AsyncClient(cookies=cookies, timeout=15) as client:
                resp = await client.get(validation_url, headers=headers)
                return resp.status_code == 200
        except Exception:
            return False

    def _token_validation_url(self) -> str | None:
        """URL to validate a bearer token. Override per airline."""
        return None

    def touch_session(self) -> None:
        """Update last_used to keep session alive. Call after successful API use."""
        self._session.touch(self.airline_name)

    def _touch_session(self) -> None:
        """Internal: update last_used without reading the file twice."""
        self._session.touch(self.airline_name)

    async def _capture_bearer_token(self, ctx: BrowserContext) -> str | None:
        """Navigate to search page and intercept the x-authorization-api header."""
        captured: list[str] = []

        async def on_request(request):
            nonlocal captured
            if captured:
                return
            url = request.url
            if "FetchAwardCalendar" in url or "FetchFlights" in url:
                token = extract_bearer_from_request_headers(request.headers)
                if token:
                    captured.append(token)

        page = await ctx.new_page()
        page.on("request", on_request)
        try:
            search_url = self._probe_search_url()
            await page.goto(search_url, wait_until="domcontentloaded", timeout=60000)
            await asyncio.sleep(5)
        except Exception:
            pass
        finally:
            page.remove_listener("request", on_request)
            await page.close()

        return captured[0] if captured else None

    def _probe_search_url(self) -> str:
        """Build a minimal search URL for token capture. Override per airline."""
        return self.login_url

    async def _save_full_session(self) -> None:
        """After successful login, persist cookies and bearer token."""
        ctx = await self._ensure_browser()
        pw_cookies = await ctx.cookies()
        # Save Playwright cookies for browser reuse
        self.cookie_file.write_text(json.dumps(pw_cookies, indent=2))
        # Save session with cookies + token
        self._session.save(
            airline=self.airline_name,
            cookies=[
                {"name": c["name"], "value": c["value"], "domain": c["domain"],
                 "path": c["path"], "httpOnly": c.get("httpOnly", False),
                 "secure": c.get("secure", False), "sameSite": c.get("sameSite", "Lax")}
                for c in pw_cookies
            ],
            bearer_token=self._bearer_token or "",
        )

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
