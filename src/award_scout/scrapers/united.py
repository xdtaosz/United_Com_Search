from __future__ import annotations

import asyncio
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

    async def _validate_token(self, token: str) -> bool:
        """Check session via browser (httpx blocked by Akamai)."""
        if not await self.load_cookies():
            return False
        ctx = await self._ensure_browser()
        page = await ctx.new_page()
        try:
            return await self._is_logged_in(page)
        finally:
            await page.close()

    async def _is_logged_in(self, page: Page) -> bool:
        try:
            await page.goto(UNITED_BASE + "/en/us/", wait_until="commit", timeout=30000)
            await asyncio.sleep(25)
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
            # United login is a modal on homepage, not a separate page
            await page.goto(UNITED_BASE + "/en/us/", wait_until="commit", timeout=30000)
            await asyncio.sleep(25)

            mp_number = settings.united_mp_number
            password = settings.united_password
            if not mp_number or not password:
                raise LoginError(
                    "UNITED_MP_NUMBER and UNITED_PASSWORD must be set in .env"
                )

            # Step 1: click "Sign in" in navbar to open login modal
            await page.evaluate("""
                const btns = document.querySelectorAll('button');
                for (const b of btns) {
                    if (b.textContent.trim() === 'Sign in') { b.click(); break; }
                }
            """)
            await asyncio.sleep(5)

            # Step 2: fill form inside the modal
            mp_field = page.locator('input[name*="MPID"], input[name*="MileagePlus"], input[name="mpNumber"]').first
            await mp_field.wait_for(state="visible", timeout=15000)
            await mp_field.fill(mp_number)

            # Step 2b: try Continue if needed (two-step form), then fill password
            continue_btn = page.locator('button:has-text("Continue")').first
            try:
                if await continue_btn.is_visible():
                    await continue_btn.click()
                    await asyncio.sleep(3)
            except Exception:
                pass

            pw_field = page.locator('input[type="password"], input[name*="password"], input[name*="Password"]').first
            await pw_field.wait_for(state="visible", timeout=30000)
            await pw_field.fill(password)

            # Step 3: click the Sign in button inside the dialog
            await page.evaluate("""
                const btns = document.querySelectorAll('button');
                for (const b of btns) {
                    if (b.textContent.trim() === 'Sign in' && b.closest('[role="dialog"]')) {
                        b.click(); break;
                    }
                }
            """)
            await asyncio.sleep(3)

            if await self._detect_mfa(page):
                code = await self._handle_mfa(page)
                if not code:
                    raise MFARequired("MFA code required but no callback provided")
                await self._submit_mfa(page, code)
                await asyncio.sleep(3)

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
        mfa_input = await page.wait_for_selector(
            'input[name="otp"], input[data-test="otp-input"], input[autocomplete="one-time-code"]',
            timeout=60000,
        )
        if mfa_input:
            await mfa_input.fill(code)

            submit_btn = await page.wait_for_selector(
                'button[type="submit"]:not([disabled])', timeout=30000
            )
            if submit_btn:
                await submit_btn.click()
                await asyncio.sleep(2)

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
        """Call FetchAwardCalendar once, return dates→(miles, cash) with availability."""
        query = SearchQuery(origin=origin, destination=destination, depart_date=start_date, cabin=cabin)
        payload = self._build_api_payload(query, calendar_length_of_stay=-1)
        cookies = self._session.get_cookies_httpx(self.airline_name) or {}
        headers = self._api_headers()

        log = SearchLogger()
        route = f"{origin.upper()}→{destination.upper()}"
        log.stage1_start(route, cabin.value, f"{start_date} +30d")

        try:
            async with httpx.AsyncClient(cookies=cookies, timeout=30, follow_redirects=True) as client:
                resp = await client.post(UNITED_FETCH_AWARD_CALENDAR, json=payload, headers=headers)
                if _is_rate_limited(resp):
                    log.error("calendar_fetch", f"RATE LIMITED (status {resp.status_code})")
                    return {}
                if resp.status_code == 200:
                    data = resp.json()
                    result = self._parse_calendar_dates(data, max_miles, log)
                    log.stage1_summary(len(result), 30)
                    return result
        except Exception as e:
            log.error("calendar_fetch", str(e)[:120])
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
        # Stage 1: calendar overview
        available = await self.get_available_dates(origin, destination, cabin, start_date, max_miles)
        if not available:
            return []

        # Stage 2: FetchFlights per qualifying date
        log = SearchLogger()
        all_offers: list[AwardOffer] = []
        current = start_date
        while current <= end_date:
            if current not in available:
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
                self._log_stage2_flights(log, offers)
                log.stage2_summary(current.isoformat(), len(offers))
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
        """FetchFlights for a single specific date."""
        payload = self._build_api_payload(query, calendar_length_of_stay=0)
        cookies = self._session.get_cookies_httpx(self.airline_name) or {}
        headers = self._api_headers()

        try:
            async with httpx.AsyncClient(cookies=cookies, timeout=30, follow_redirects=True) as client:
                resp = await client.post(UNITED_FETCH_FLIGHTS, json=payload, headers=headers)
                if _is_rate_limited(resp):
                    raise RateLimitError(f"Rate limited on {query.depart_date} (status {resp.status_code})")
                if resp.status_code == 200:
                    data = resp.json()
                    if data.get("Status") == 200:
                        return self._parse_fetch_response(data, query)
        except RateLimitError:
            raise
        except Exception:
            pass
        return []

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
        page = await ctx.new_page()
        page.set_default_timeout(settings.browser_timeout_ms)
        api_responses: list[dict[str, Any]] = []

        async def capture_api(response):
            if response.status == 200 and (
                UNITED_FETCH_AWARD_CALENDAR in response.url
                or UNITED_FETCH_FLIGHTS in response.url
            ):
                try:
                    api_responses.append(await response.json())
                except Exception:
                    pass

        page.on("response", capture_api)
        try:
            search_url = self._build_search_url(query)
            await page.goto(search_url, wait_until="commit", timeout=60000)
            await asyncio.sleep(25)

            offers: list[AwardOffer] = []
            for resp in api_responses:
                parsed = self._parse_fetch_response(resp, query)
                offers.extend(parsed)
            return offers
        finally:
            page.remove_listener("response", capture_api)
            await page.close()

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
