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
from award_scout.scrapers.base import BaseAirlineScraper, LoginError, MFARequired

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
        return UNITED_LOGIN

    # --- Login ---

    def _token_validation_url(self) -> str | None:
        return f"{UNITED_BASE}/api/auth/validate-token"

    async def _is_logged_in(self, page: Page) -> bool:
        try:
            await page.goto(f"{UNITED_BASE}/en/us/account", wait_until="domcontentloaded")
            await asyncio.sleep(1)
            content = await page.content()
            return "mileageplus-number" in content.lower() or "sign out" in content.lower()
        except Exception:
            return False

    async def _do_login(self) -> bool:
        ctx = await self._ensure_browser()
        page = await ctx.new_page()
        page.set_default_timeout(settings.browser_timeout_ms)

        try:
            await page.goto(UNITED_LOGIN, wait_until="domcontentloaded", timeout=90000)
            await asyncio.sleep(3)

            mp_number = settings.united_mp_number
            password = settings.united_password
            if not mp_number or not password:
                raise LoginError(
                    "UNITED_MP_NUMBER and UNITED_PASSWORD must be set in .env"
                )

            mp_field = await page.wait_for_selector(
                'input[name="mpNumber"], input[data-test="mpNumber-input"], #mpNumber',
                timeout=15000,
            )
            if mp_field:
                await mp_field.fill(mp_number)
            else:
                raise LoginError("Could not find MileagePlus number field")

            pw_field = await page.wait_for_selector(
                'input[name="password"], input[data-test="password-input"], #password',
                timeout=5000,
            )
            if pw_field:
                await pw_field.fill(password)

            submit_btn = await page.wait_for_selector(
                'button[type="submit"], button[data-test="sign-in-button"]', timeout=5000
            )
            if submit_btn:
                await submit_btn.click()

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
            timeout=10000,
        )
        if mfa_input:
            await mfa_input.fill(code)

            submit_btn = await page.wait_for_selector(
                'button[type="submit"]:not([disabled])', timeout=5000
            )
            if submit_btn:
                await submit_btn.click()
                await asyncio.sleep(2)

    # --- Search ---

    async def search(self, query: SearchQuery) -> list[AwardOffer]:
        if not await self.login():
            raise LoginError("Cannot search without successful login")

        if self._bearer_token:
            offers = await self._search_via_api(query)
            if offers:
                self.touch_session()
                return offers

        return await self._search_via_browser(query)

    def _probe_search_url(self) -> str:
        """Build a minimal award search URL that triggers FetchAwardCalendar."""
        params = {
            "f": "SFO",
            "t": "ORD",
            "d": date.today().strftime("%Y/%m/%d"),
            "tt": "1",
            "at": "1",
            "sc": "3",
            "act": "2",
            "px": "1",
            "tqp": "A",
        }
        return f"{UNITED_FSR_SEARCH}?{urllib.parse.urlencode(params)}"

    # --- API-first search (httpx) ---

    async def _search_via_api(self, query: SearchQuery) -> list[AwardOffer]:
        await _rate_limit_pause()
        cookies = self._session.get_cookies_httpx(self.airline_name) or {}
        headers = {
            "Content-Type": "application/json",
            "x-authorization-api": f"bearer {self._bearer_token}",
            "Origin": UNITED_BASE,
            "Referer": f"{UNITED_BASE}/en/us/fsr/choose-flights",
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
        }

        payload = self._build_api_payload(query)
        try:
            async with httpx.AsyncClient(cookies=cookies, timeout=30, follow_redirects=True) as client:
                resp = await client.post(
                    UNITED_FETCH_AWARD_CALENDAR,
                    json=payload,
                    headers=headers,
                )
                if resp.status_code == 200:
                    data = resp.json()
                    if data.get("Status") == 200:
                        return self._parse_fetch_award_calendar(data, query)
        except Exception:
            pass
        return []

    def _build_api_payload(self, query: SearchQuery) -> dict[str, Any]:
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
            "CalendarLengthOfStay": -1,
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
            await page.goto(search_url, wait_until="domcontentloaded", timeout=90000)
            await asyncio.sleep(5)

            offers: list[AwardOffer] = []
            for resp in api_responses:
                parsed = self._parse_fetch_award_calendar(resp, query)
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

    def _parse_fetch_award_calendar(
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
                    miles = int(prod.get("AwardMiles", prod.get("Miles", 0)) or 0)
                    if miles == 0:
                        continue
                    taxes = float(prod.get("Cash", prod.get("TotalPrice", 0)) or 0)
                    cabin_str = prod.get("Cabin", prod.get("CabinType", "Economy"))
                    cabin = CabinClass.from_united_code(cabin_str)
                    seats = prod.get("SeatsRemaining", prod.get("AvailableSeats", 1))
                    fare_class = prod.get("FareClass", prod.get("BookingCode", ""))
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
    def _parse_flight_segments(flight: dict[str, Any]) -> list[FlightSegment]:
        segments: list[FlightSegment] = []
        conns = flight.get("Connections", [])
        if not conns:
            # Nonstop: build single segment from flight-level fields
            dep_dt = flight.get("DepartDateTime", "")
            arr_dt = flight.get("DestinationDateTime", "")
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
            for c in conns:
                segments.append(FlightSegment(
                    airline=c.get("Carrier", ""),
                    flight_number=str(c.get("FlightNumber", "")),
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
        results: dict[str, list[AwardOffer]] = {}
        current = start_date
        while current <= end_date:
            query = SearchQuery(
                origin=origin,
                destination=destination,
                depart_date=current,
                cabin=cabin,
            )
            offers = await self.search(query)
            results[current.isoformat()] = offers
            current += timedelta(days=1)
        return results


async def _rate_limit_pause() -> None:
    base_delay = settings.search_delay_seconds
    jitter = random.uniform(0, base_delay * 0.5)
    await asyncio.sleep(base_delay + jitter)
