from __future__ import annotations

import asyncio
import json
import random
import urllib.parse
from datetime import date, timedelta
from typing import Any, Optional

import httpx
from playwright.async_api import Page

from award_scout.config import settings
from award_scout.models import (
    Airline,
    AwardOffer,
    CabinClass,
    FlightSegment,
    SearchQuery,
)
from award_scout.scrapers.base import BaseAirlineScraper, LoginError, MFARequired, RateLimitError
from award_scout.search_logger import SearchLogger

UNITED_BASE = "https://www.united.com"
UNITED_LOGIN = f"{UNITED_BASE}/en/us/login"

UNITED_FETCH_AWARD_CALENDAR = f"{UNITED_BASE}/api/flight/FetchAwardCalendar"
UNITED_FETCH_FLIGHTS = f"{UNITED_BASE}/api/flight/FetchFlights"
UNITED_FSR_SEARCH = f"{UNITED_BASE}/en/us/fsr/choose-flights"


class UnitedScraper(BaseAirlineScraper):
    def __init__(self):
        super().__init__(Airline.UNITED.value)

    @property
    def login_url(self) -> str:
        return UNITED_BASE + "/en/us/"

    # --- Login ---

    def _token_validation_url(self) -> str | None:
        return f"{UNITED_BASE}/api/auth/validate-token"

    async def login(self) -> bool:
        """Override: check session file first, skip browser if valid."""
        # 1. Session file — if recent, use it without any network call
        saved = self._session.load(self.airline_name)
        if saved:
            token = saved.get("bearer_token")
            if token:
                self._bearer_token = token
                self._touch_session()
                return True

        # 2. Cookie-based login (load cookies, verify via browser)
        if await self.load_cookies():
            ctx = await self._ensure_browser()
            page = await ctx.new_page()
            try:
                await page.goto(self.login_url, wait_until="commit", timeout=30000)
                await asyncio.sleep(20)
                if await self._is_logged_in(page):
                    await page.close()
                    self._bearer_token = await self._capture_bearer_token(ctx)
                    if self._bearer_token:
                        await self._save_full_session()
                    return True
            except Exception:
                pass
            await page.close()

        # 3. Full login with credentials
        return await self._do_login()

    async def _is_logged_in(self, page: Page) -> bool:
        try:
            await page.goto(UNITED_BASE + "/en/us/", wait_until="commit", timeout=30000)
            await asyncio.sleep(20)
            content = await page.content()
            # Only check markers that appear only when truly authenticated:
            # "Cardmember" badge, or auth-specific session indicator
            return "Cardmember" in content or '"isLoggedIn":true' in content
        except Exception:
            return False

    async def _do_login(self) -> bool:
        ctx = await self._ensure_browser()
        page = await ctx.new_page()
        page.set_default_timeout(settings.browser_timeout_ms)

        try:
            mp_number = settings.united_mp_number
            password = settings.united_password
            if not mp_number or not password:
                raise LoginError("UNITED_MP_NUMBER and UNITED_PASSWORD must be set in .env")

            # Navigate to login page and wait for React to render
            await page.goto(UNITED_BASE + "/en/us/login", wait_until="commit", timeout=30000)
            await asyncio.sleep(50)

            # If page hasn't rendered yet, wait longer
            if await page.locator('button').count() == 0:
                await asyncio.sleep(30)
                print("  Extra wait for slow render...")
            print(f"  Buttons on page: {await page.locator('button').count()}")

            # Click Sign in button to open modal
            for selector in [
                'button:has-text("Sign in")',
                'button:has-text("Sign In")',
            ]:
                try:
                    btn = page.locator(selector).first
                    if await btn.count() > 0 and await btn.is_visible():
                        await btn.click()
                        await asyncio.sleep(5)
                        break
                except Exception:
                    pass

            # Debug: check page state
            print(f"  Buttons: {await page.locator('button').count()}")
            try:
                for frame in page.frames:
                    frame_inputs = await frame.locator('input:visible').count()
                    if frame_inputs > 0:
                        print(f"  Frame {frame.url[:60]}: {frame_inputs} inputs")
            except Exception:
                pass

            # Find the login field (MP/email) — NOT the search box
            mp_field = page.locator(
                'input[name*="MPID"], input[name*="MileagePlus"], '
                'input[name*="mpNumber"], input[name*="email"], '
                'input[id*="email"], input[type="email"]'
            ).first
            mp_visible = await mp_field.count() > 0
            if mp_visible:
                mp_visible = await mp_field.is_visible()

            if mp_visible:
                await mp_field.fill(mp_number)
                # Press Enter or click Continue/Next
                await mp_field.press("Enter")
                await asyncio.sleep(5)
                # Also try clicking
                for btn_text in ["Continue", "Next", "Sign in"]:
                    try:
                        btn = page.locator(f'button:has-text("{btn_text}")').first
                        if await btn.count() > 0 and await btn.is_visible():
                            await btn.click()
                            await asyncio.sleep(3)
                            break
                    except Exception:
                        pass

            # Find password field (may be visible already if MP remembered)
            pw_input = page.locator('input[type="password"]').first
            try:
                await pw_input.wait_for(state="visible", timeout=20000)
            except Exception:
                # Secondary scan
                all_inputs = page.locator('input:visible')
                for i in range(await all_inputs.count()):
                    inp = all_inputs.nth(i)
                    t = await inp.get_attribute('type') or ''
                    if t == 'password':
                        pw_input = inp
                        break
                else:
                    await page.screenshot(path=str(settings.data_path / "login_failed.png"))
                    raise LoginError("Cannot find password field")

            await pw_input.fill(password)

            # Click Sign in button in dialog
            submit = page.locator('button:has-text("Sign in")').last
            if await submit.count() > 0:
                await submit.click()
                await asyncio.sleep(5)

            if await self._detect_mfa(page):
                code = await self._handle_mfa(page)
                if not code:
                    raise MFARequired("MFA code required but no callback provided")
                try:
                    await self._submit_mfa(page, code)
                    await asyncio.sleep(3)
                except Exception:
                    pass

            if not await self._is_logged_in(page):
                raise LoginError("Login failed — check credentials or MFA")

            await self.save_cookies()
            self._bearer_token = await self._capture_bearer_token(ctx)
            if self._bearer_token:
                await self._save_full_session()
            return True

        finally:
            await page.close()

    async def _detect_mfa(self, page: Page) -> bool:
        content = await page.content()
        mfa_indicators = [
            "verification code",
            "two-factor",
            "multi-factor",
            "sms code",
            "security code",
            "verify your identity",
            "mfa",
        ]
        return any(indicator in content.lower() for indicator in mfa_indicators)

    async def _handle_mfa(self, page: Page) -> Optional[str]:
        if self._mfa_callback:
            return await self._mfa_callback()
        return None

    async def _submit_mfa(self, page: Page, code: str) -> None:
        # Try multiple selectors for MFA input
        mfa_sel = (
            'input[name="otp"], input[data-test="otp-input"], '
            'input[autocomplete="one-time-code"], '
            'input[type="text"]:visible, input[type="tel"]:visible, '
            'input[id*="code"]:visible, input[id*="otp"]:visible, '
            'input[name*="code"]:visible, input[name*="verif"]:visible'
        )
        mfa_input = await page.wait_for_selector(mfa_sel, timeout=60000)
        if mfa_input:
            await mfa_input.fill(code)

            # Try multiple submit buttons
            submit_btn = await page.wait_for_selector(
                'button[type="submit"]:not([disabled]), '
                'button:has-text("Verify"):visible, '
                'button:has-text("Submit"):visible, '
                'button:has-text("Confirm"):visible',
                timeout=30000,
            )
            if submit_btn:
                await submit_btn.click()
                await asyncio.sleep(3)

    # --- Search ---

    async def search(self, query: SearchQuery) -> list[AwardOffer]:
        """Search a single date. Uses FetchFlights for detailed results."""
        if not await self.login():
            raise LoginError("Cannot search without successful login")

        if self._bearer_token:
            offers = await self._search_single_date(query)
            if offers:
                self.touch_session()
                return offers

        return await self._search_via_browser(query)

    async def get_available_dates(
        self,
        origin: str,
        destination: str,
        cabin: CabinClass,
        start_date: date,
        max_miles: int | None = None,
    ) -> dict[date, tuple[int, float]]:
        """Call FetchAwardCalendar via browser, return dates→(miles, cash) with availability."""
        log = SearchLogger()
        route = f"{origin.upper()}→{destination.upper()}"
        cabin_name = cabin.value
        mm_str = f" ≤{max_miles:,}mi" if max_miles else ""
        log.stage1_start(route, cabin_name, f"{start_date} +30d{mm_str}")
        print(f"  [CALENDAR] browser: {route} {cabin_name} {start_date}")

        try:
            ctx = await self._ensure_browser()
            # Load saved cookies for authentication
            if self.cookie_file.exists():
                cookies = json.loads(self.cookie_file.read_text())
                await ctx.add_cookies(cookies)
                print(f"  [CALENDAR] loaded {len(cookies)} cookies")
            page = await ctx.new_page()

            # Build calendar search URL
            calendar_url = (
                f"https://www.united.com/en/us/fsr/choose-flights"
                f"?f={origin.upper()}&t={destination.upper()}"
                f"&d={start_date.strftime('%Y/%m/%d')}"
                f"&tt=1&at=1&sc=7&act=2&px=1&tqp=A"
            )
            print(f"  [CALENDAR] navigating...")
            await page.goto(calendar_url, wait_until="commit", timeout=60000)
            await asyncio.sleep(25)

            # If page shows login form, fill password automatically
            pw = page.locator('input[type="password"]').first
            if await pw.count() > 0 and await pw.is_visible():
                print(f"  [CALENDAR] login required, filling password...")
                await pw.fill(settings.united_password or "")
                signin = page.locator('button:has-text("Sign in")').last
                if await signin.count() > 0 and await signin.is_visible():
                    await signin.click()
                    await asyncio.sleep(5)
                    # Re-navigate to trigger search after login
                    await page.goto(calendar_url, wait_until="commit", timeout=60000)
                    await asyncio.sleep(20)

            # Click Update to trigger calendar API
            async with page.expect_response(
                lambda r: r.status == 200 and 'FetchAwardCalendar' in r.url,
                timeout=90000
            ) as resp_info:
                btn = page.locator('button:has-text("Update"), button:has-text("Find flights")').first
                if await btn.count() > 0 and await btn.is_visible():
                    await btn.click()
                    print(f"  [CALENDAR] clicked search button...")
                else:
                    print(f"  [CALENDAR] search button not found, waiting...")
            resp = await resp_info.value
            data = await resp.json()
            result = self._parse_calendar_dates(data, max_miles, log)
            log.stage1_summary(len(result), 30)
            print(f"  [CALENDAR] {len(result)} qualifying dates")
            await page.close()
            return result
        except Exception as e:
            print(f"  [CALENDAR] failed (will query all dates individually): {e}")
            if page:
                await page.close()
            return {}

    async def search_range(
        self,
        origin: str,
        destination: str,
        start_date: date,
        end_date: date,
        cabin: CabinClass,
        max_miles: int | None = None,
    ) -> list[AwardOffer]:
        """Calendar pre-filter → per-day FetchFlights with delays."""
        log = SearchLogger()
        route = f"{origin.upper()}→{destination.upper()}"
        cabin_name = cabin.value
        mm_str = f" ≤{max_miles:,}mi" if max_miles else " no limit"
        log._write(f"[SEARCH] {route} | {cabin_name} | {start_date} to {end_date}{mm_str} | delay={settings.search_delay_seconds}s")

        # Stage 1: calendar overview
        available = await self.get_available_dates(origin, destination, cabin, start_date, max_miles)

        # Stage 2: FetchFlights per qualifying date (or all dates if calendar failed)
        log = SearchLogger()
        all_offers: list[AwardOffer] = []
        current = start_date
        while current <= end_date:
            if available and current not in available:
                current += timedelta(days=1)
                continue

            log.stage2_start(current.isoformat())
            query = SearchQuery(
                origin=origin, destination=destination, depart_date=current, cabin=cabin,
            )
            try:
                offers = await self._search_single_date(query)
            except RateLimitError as e:
                log.error("stage2", f"RATE LIMITED at {current.isoformat()} — stopping batch search. {e}")
                break
            if offers:
                all_offers.extend(offers)
                # Log raw counts (before MNL filtering)
                mnl_count = sum(1 for o in offers if any(
                    s.departure_airport == "MNL" or s.arrival_airport == "MNL"
                    for s in o.segments
                ))
                log._write(f"[STAGE2] {current.isoformat()}: {len(offers)} flights found (incl {mnl_count} via MNL)")
                self._log_stage2_flights(log, offers)
            else:
                log.stage2_empty(current.isoformat())

            current += timedelta(days=1)
            if current <= end_date:
                await _rate_limit_pause()

        return all_offers

    @staticmethod
    def _log_stage2_flights(log: SearchLogger, offers: list[AwardOffer]) -> None:
        for o in offers:
            for seg in o.segments:
                route = f"{seg.departure_airport}→{seg.arrival_airport}"
                times = f"{_short_time(seg.departure_time)}–{_short_time(seg.arrival_time)}"
                dur = f"{seg.duration_minutes // 60}h{seg.duration_minutes % 60}m"
                log.stage2_flight(
                    flight_num=seg.flight_number,
                    route=route,
                    times=times,
                    duration=dur,
                    stops=o.stops,
                    miles=o.miles_required,
                    cash=o.taxes_fees,
                    cabin=o.cabin.value,
                    seats=o.total_seats_available,
                    fare_class=seg.fare_class or "",
                )

    def _probe_search_url(self) -> str:
        params = {
            "f": "SFO", "t": "ORD",
            "d": date.today().strftime("%Y/%m/%d"),
            "tt": "1", "at": "1", "sc": "3", "act": "2", "px": "1", "tqp": "A",
        }
        return f"{UNITED_FSR_SEARCH}?{urllib.parse.urlencode(params)}"

    # --- API helpers ---

    async def _search_single_date(self, query: SearchQuery) -> list[AwardOffer]:
        """FetchFlights via browser for a single date."""
        print(f"  [FETCH] browser: {query.depart_date}")
        return await self._search_via_browser(query)

    def _api_headers(self) -> dict[str, str]:
        return {
            "Content-Type": "application/json",
            "x-authorization-api": f"bearer {self._bearer_token}",
            "Origin": UNITED_BASE,
            "Referer": f"{UNITED_BASE}/en/us/fsr/choose-flights",
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
        }

    def _build_api_payload(self, query: SearchQuery, calendar_length_of_stay: int = -1) -> dict[str, Any]:
        is_business = query.cabin in (CabinClass.BUSINESS, CabinClass.FIRST)
        fare_family = "BUSINESS" if is_business else "ECO"
        cabin_main = "premium" if is_business else "eco"

        return {
            "SearchTypeSelection": 1,
            "SortType": "bestmatches",
            "Trips": [{
                "Origin": query.origin.upper(),
                "Destination": query.destination.upper(),
                "DepartDate": query.depart_date.strftime("%Y/%m/%d"),
                "Index": 1,
                "TripIndex": 1,
                "SearchRadiusMilesOrigin": 0,
                "SearchRadiusMilesDestination": 0,
                "DepartTimeApprox": 0,
                "SearchFiltersIn": {
                    "FareFamily": fare_family,
                    "AirportsStop": None,
                    "AirportsStopToAvoid": None,
                    "ShopIndicators": {
                        "IsTravelCreditsApplied": False,
                        "IsDoveFlow": True,
                    },
                },
            }],
            "CabinPreferenceMain": cabin_main,
            "PaxInfoList": [{"PaxType": 1}],
            "AwardTravel": True,
            "NGRP": True,
            "CalendarLengthOfStay": calendar_length_of_stay,
            "PetCount": 0,
            "FareType": "mixedtoggle",
            "BuildHashValue": "true",
        }

    # --- Browser-based search (fallback) ---

    async def _search_via_browser(self, query: SearchQuery) -> list[AwardOffer]:
        ctx = await self._ensure_browser()
        if self.cookie_file.exists():
            cookies = json.loads(self.cookie_file.read_text())
            await ctx.add_cookies(cookies)
        page = await ctx.new_page()
        page.set_default_timeout(settings.browser_timeout_ms)

        search_url = self._build_search_url(query)

        # If page shows login form, fill password
        await page.goto(search_url, wait_until="commit", timeout=60000)
        await asyncio.sleep(20)
        pw = page.locator('input[type="password"]').first
        if await pw.count() > 0 and await pw.is_visible():
            await pw.fill(settings.united_password or "")
            btn = page.locator('button:has-text("Sign in")').last
            if await btn.count() > 0:
                await btn.click()
                await asyncio.sleep(5)
            await page.goto(search_url, wait_until="commit", timeout=60000)
            await asyncio.sleep(20)
        try:
            async with page.expect_response(
                lambda r: r.status == 200 and 'FetchFlights' in r.url,
                timeout=45000
            ) as resp_info:
                await page.goto(search_url, wait_until="commit", timeout=60000)
            resp = await resp_info.value
            data = await resp.json()
            trips = (data.get("data", data)).get("Trips", [])
            for t in trips:
                flights = t.get("Flights", [])
                total_products = sum(len(f.get("Products", [])) for f in flights)
                cabins = set()
                for f in flights:
                    for p in (f.get("Products", []) or []):
                        cabins.add(p.get("CabinType", "?"))
                print(f"  [RAW] {t.get('DepartDate','?')}: {len(flights)} flights, {total_products} products, cabins: {sorted(cabins)}")
            parsed = self._parse_fetch_response(data, query)
            await page.close()
            return parsed
        except Exception as e:
            print(f"  [FETCH] expect_response failed: {e}")
            await page.close()
            return []
            return []

    def _build_search_url(self, query: SearchQuery) -> str:
        sc = "7" if query.cabin in (CabinClass.BUSINESS, CabinClass.FIRST) else "3"
        params = {
            "f": query.origin.upper(),
            "t": query.destination.upper(),
            "d": query.depart_date.strftime("%Y/%m/%d"),
            "tt": "1",
            "at": "1",
            "sc": sc,
            "act": "2",
            "px": str(query.passengers),
            "tqp": "A",
        }
        if query.return_date:
            params["rd"] = query.return_date.strftime("%Y/%m/%d")
            params["tt"] = "2"
        return f"{UNITED_FSR_SEARCH}?{urllib.parse.urlencode(params)}"

    # --- Response Parsing ---

    @staticmethod
    def _parse_calendar_dates(
        data: dict[str, Any],
        max_miles: int | None = None,
        log: SearchLogger | None = None,
    ) -> dict[date, tuple[int, float]]:
        """Extract dates with award availability from FetchAwardCalendar response.
        Returns {date: (miles, cash)}. Only includes Business/First cabins."""
        dates: dict[date, tuple[int, float]] = {}
        d = data.get("data", data)
        total_biz = 0
        for trip in d.get("Trips", []):
            for flight in trip.get("Flights", []):
                products = flight.get("Products") or flight.get("Fares") or []
                for prod in products:
                    cabin_type = prod.get("CabinType", "")
                    ctx = prod.get("Context", {})
                    ngrp_miles = int(ctx.get("NgrpMiles", 0) or 0)
                    pax_prices = ctx.get("PaxPrices", [])
                    pax_miles = int(pax_prices[0].get("Miles", 0) if pax_prices else 0)
                    miles = ngrp_miles or pax_miles
                    if miles == 0:
                        continue
                    if cabin_type in ("Business", "BusinessFirst"):
                        total_biz += 1
        print(f"  [CALENDAR] raw: {total_biz} Business products across all dates")
        
        for trip in d.get("Trips", []):
            for flight in trip.get("Flights", []):
                products = flight.get("Products") or flight.get("Fares") or []
                for prod in products:
                    cabin_type = prod.get("CabinType", "")
                    if cabin_type not in ("Business", "BusinessFirst"):
                        continue
                    ctx = prod.get("Context", {})
                    ngrp_miles = int(ctx.get("NgrpMiles", 0) or 0)
                    pax_prices = ctx.get("PaxPrices", [])
                    pax_miles = int(pax_prices[0].get("Miles", 0) if pax_prices else 0)
                    miles = ngrp_miles or pax_miles
                    if miles == 0:
                        continue
                    cash = float((ctx.get("ReferenceFare") or {}).get("Amount", 0) or 0)
                    dep = flight.get("DepartDateTime", "")
                    if not dep:
                        continue
                    try:
                        d_date = date.fromisoformat(dep[:10])
                    except ValueError:
                        continue
                    if max_miles is not None and miles > max_miles:
                        if log:
                            log.stage1_miss(dep[:10], miles)
                        continue
                    dates[d_date] = (miles, cash)
                    if log:
                        log.stage1_hit(dep[:10], miles, cash)
                    break  # one product per day is enough for availability check
        return dates

    def _parse_fetch_response(
        self, data: dict[str, Any], query: SearchQuery
    ) -> list[AwardOffer]:
        offers: list[AwardOffer] = []
        d = data.get("data", data)
        for trip in d.get("Trips", []):
            depart_date = trip.get("DepartDate", "")
            for flight in trip.get("Flights", []):
                products = (
                    flight.get("Products")
                    or flight.get("Fares")
                    or []
                )
                segments = self._parse_flight_segments(flight)
                for prod in products:
                    ctx = prod.get("Context", {})
                    pax_prices = ctx.get("PaxPrices", [])
                    ngrp_miles = int(ctx.get("NgrpMiles", 0) or 0)
                    pax_miles = int(pax_prices[0].get("Miles", 0) if pax_prices else 0)
                    miles = ngrp_miles or pax_miles
                    if miles == 0:
                        continue
                    ref_fare = ctx.get("ReferenceFare", {})
                    taxes = float(ref_fare.get("Amount", 0) or 0)
                    cabin_str = prod.get("CabinType", "Economy")
                    cabin = CabinClass.from_united_code(cabin_str)
                    fare_class = prod.get("BookingCode", "")
                    seats = _parse_seats(flight.get("BookingClassAvailability", ""), fare_class)
                    total_dur = sum(s.duration_minutes for s in segments) if segments else 0
                    stops = len(segments) - 1 if segments else 0

                    offers.append(AwardOffer(
                        source_airline=Airline.UNITED.value,
                        query_origin=query.origin.upper(),
                        query_destination=query.destination.upper(),
                        depart_date=depart_date or query.depart_date.isoformat(),
                        return_date=query.return_date.isoformat() if query.return_date else None,
                        segments=segments,
                        total_duration_minutes=total_dur,
                        stops=stops,
                        miles_required=miles,
                        taxes_fees=taxes,
                        cabin=cabin,
                        total_seats_available=seats or 1,
                        raw_data=prod,
                    ))
        return offers

    @staticmethod
    @staticmethod
    def _parse_flight_segments(flight: dict[str, Any]) -> list[FlightSegment]:
        segments: list[FlightSegment] = []
        conns = flight.get("Connections", [])
        dep_dt = flight.get("DepartDateTime", "")
        arr_dt = flight.get("DestinationDateTime", "")

        if not conns:
            segments.append(FlightSegment(
                airline=flight.get("MarketingCarrier", ""),
                flight_number=str(flight.get("FlightNumber", "")),
                departure_airport=flight.get("Origin", ""),
                arrival_airport=flight.get("Destination", ""),
                departure_time=dep_dt,
                arrival_time=arr_dt,
                duration_minutes=int(flight.get("TravelMinutes", 0)),
                aircraft=flight.get("Equipment", ""),
            ))
        else:
            # First leg: Origin → first connection
            first_conn = conns[0]
            segments.append(FlightSegment(
                airline=flight.get("MarketingCarrier", ""),
                flight_number=str(flight.get("FlightNumber", "")),
                departure_airport=flight.get("Origin", ""),
                arrival_airport=first_conn.get("Origin", ""),
                departure_time=dep_dt,
                arrival_time=first_conn.get("DepartureTime", first_conn.get("DepartDateTime", "")),
                duration_minutes=0,
                aircraft=flight.get("Equipment", ""),
            ))
            # Remaining legs: connections
            for c in conns:
                segments.append(FlightSegment(
                    airline=c.get("Carrier", flight.get("MarketingCarrier", "")),
                    flight_number=str(c.get("FlightNumber", flight.get("FlightNumber", ""))),
                    departure_airport=c.get("Origin", ""),
                    arrival_airport=c.get("Destination", ""),
                    departure_time=c.get("DepartureTime", c.get("DepartDateTime", "")),
                    arrival_time=c.get("ArrivalTime", c.get("ArriveDateTime", "")),
                    duration_minutes=int(c.get("Duration", 0)),
                    aircraft=c.get("Equipment", ""),
                ))
        return segments

    # --- Date range ---

    async def search_route_range(
        self,
        origin: str,
        destination: str,
        start_date: date,
        end_date: date,
        cabin: CabinClass = CabinClass.ECONOMY,
    ) -> dict[str, list[AwardOffer]]:
        offers = await self.search_range(origin, destination, start_date, end_date, cabin)
        results: dict[str, list[AwardOffer]] = {}
        for o in offers:
            results.setdefault(o.depart_date, []).append(o)
        return results


async def _rate_limit_pause() -> None:
    base_delay = settings.search_delay_seconds
    jitter = random.uniform(0, base_delay * 0.5)
    await asyncio.sleep(base_delay + jitter)


def _short_time(iso_str: str) -> str:
    """Extract HH:MM from ISO8601 or raw time string."""
    if not iso_str:
        return "?"
    if "T" in iso_str:
        return iso_str[11:16]
    return iso_str[:5]


def _is_rate_limited(resp) -> bool:
    """Check if an httpx response indicates rate limiting or access denied."""
    if resp.status_code in (403, 428, 429):
        return True
    content_type = resp.headers.get("content-type", "")
    if "text/html" in content_type and resp.status_code != 200:
        return True
    return False
    """Extract seat count from BookingClassAvailability string.
    Format: 'J9|JN9|C9|...|XN4|X0' → for XN, returns 4.
    """
    if not availability or not fare_class:
        return 0
    for part in availability.split("|"):
        if part.startswith(fare_class) and len(part) > len(fare_class):
            try:
                return int(part[len(fare_class):])
            except ValueError:
                pass
    return 0
