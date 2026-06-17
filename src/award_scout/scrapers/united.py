from __future__ import annotations

import asyncio
from datetime import date, timedelta
from typing import Any, Optional
from urllib.parse import urlencode

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
UNITED_AWARD_CALENDAR_API = (
    "https://www.united.com/api/flight/availability/awardCalendar"
)
UNITED_SEARCH_API = "https://www.united.com/api/flight/availability/v2"


class UnitedScraper(BaseAirlineScraper):
    def __init__(self):
        super().__init__(Airline.UNITED.value)

    @property
    def login_url(self) -> str:
        return UNITED_LOGIN

    # --- Login ---

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

            # Fill login form
            # United's login has multiple iframes/fields, try multiple selectors
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

            # Click submit
            submit_btn = await page.wait_for_selector(
                'button[type="submit"], button[data-test="sign-in-button"]', timeout=5000
            )
            if submit_btn:
                await submit_btn.click()

            # Wait for navigation after login
            await asyncio.sleep(3)

            # Check for MFA challenge
            page_content = await page.content()
            if await self._detect_mfa(page):
                code = await self._handle_mfa(page)
                if not code:
                    raise MFARequired("MFA code required but no callback provided")
                await self._submit_mfa(page, code)
                await asyncio.sleep(3)

            # Verify login success
            if not await self._is_logged_in(page):
                raise LoginError("Login failed — check credentials or MFA")

            await self.save_cookies()
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
        """Search United award availability using the award calendar API.

        The award calendar endpoint returns ~30 days of pricing in one request.
        We intercept the API response via Playwright route capture.
        """
        if not await self.login():
            raise LoginError("Cannot search without successful login")

        ctx = await self._ensure_browser()
        page = await ctx.new_page()
        page.set_default_timeout(settings.browser_timeout_ms)

        offers: list[AwardOffer] = []
        api_responses: list[dict[str, Any]] = []

        async def capture_api_response(response):
            if UNITED_AWARD_CALENDAR_API in response.url and response.status == 200:
                try:
                    body = await response.json()
                    api_responses.append(body)
                except Exception:
                    pass

        page.on("response", capture_api_response)

        try:
            search_url = self._build_calendar_url(query)
            await page.goto(search_url, wait_until="domcontentloaded", timeout=90000)
            await asyncio.sleep(5)

            if not api_responses:
                await self._trigger_search_via_ui(page)
                await asyncio.sleep(5)

            for resp_data in api_responses:
                parsed = self._parse_calendar_response(resp_data, query)
                offers.extend(parsed)

            if not offers:
                offers = await self._search_v2_api(page, query)

            return offers

        finally:
            page.remove_listener("response", capture_api_response)
            await page.close()

    def _build_calendar_url(self, query: SearchQuery) -> str:
        params: dict[str, str] = {
            "origin": query.origin.upper(),
            "destination": query.destination.upper(),
            "departDate": query.depart_date.isoformat(),
            "returnDate": query.return_date.isoformat() if query.return_date else "",
            "cabin": self._united_cabin_code(query.cabin),
            "passengers": str(query.passengers),
        }
        return f"{UNITED_BASE}/en/us/booking/flights/award/calendar?{urlencode(params)}"

    @staticmethod
    def _united_cabin_code(cabin: CabinClass) -> str:
        mapping = {
            CabinClass.ECONOMY: "economy",
            CabinClass.PREMIUM_ECONOMY: "premium",
            CabinClass.BUSINESS: "business",
            CabinClass.FIRST: "first",
        }
        return mapping.get(cabin, "economy")

    async def _trigger_search_via_ui(self, page: Page) -> None:
        """Fallback: interact with the search form to trigger API calls."""
        try:
            submit = await page.wait_for_selector(
                'button[type="submit"], button[data-test="award-search-button"]',
                timeout=8000,
            )
            if submit:
                await submit.click()
                await asyncio.sleep(3)
        except Exception:
            pass

    async def _search_v2_api(
        self, page: Page, query: SearchQuery
    ) -> list[AwardOffer]:
        """Direct API call using the authenticated session's cookies/tokens."""
        api_responses: list[dict[str, Any]] = []

        async def capture_v2(response):
            if UNITED_SEARCH_API in response.url and response.status == 200:
                try:
                    body = await response.json()
                    api_responses.append(body)
                except Exception:
                    pass

        page.on("response", capture_v2)
        try:
            await page.goto(self._build_calendar_url(query), wait_until="domcontentloaded", timeout=90000)
            await asyncio.sleep(5)
            offers = []
            for resp in api_responses:
                offers.extend(self._parse_v2_response(resp, query))
            return offers
        finally:
            page.remove_listener("response", capture_v2)

    # --- Response Parsing ---

    def _parse_calendar_response(
        self, data: dict[str, Any], query: SearchQuery
    ) -> list[AwardOffer]:
        """Parse the award calendar API response."""
        offers: list[AwardOffer] = []

        calendar_days = data.get("data", {}).get("calendarDays", [])
        if not calendar_days:
            calendar_days = data.get("calendarDays", [])

        for day in calendar_days:
            depart_date_str = day.get("departDate", "")
            if not depart_date_str:
                continue

            trips = day.get("trips", [])
            for trip in trips:
                offers.extend(
                    self._parse_trip(trip, query, depart_date_str)
                )

        return offers

    def _parse_v2_response(self, data: dict[str, Any], query: SearchQuery) -> list[AwardOffer]:
        """Parse the v2 flight search API response."""
        offers: list[AwardOffer] = []

        trips = data.get("data", {}).get("trips", [])
        for trip in trips:
            depart_date_str = trip.get("departDate", "")
            for bound in trip.get("bounds", []):
                offers.extend(
                    self._parse_bound(bound, query, depart_date_str)
                )

        return offers

    def _parse_trip(
        self, trip: dict[str, Any], query: SearchQuery, depart_date_str: str
    ) -> list[AwardOffer]:
        results: list[AwardOffer] = []
        products = trip.get("products", [])
        if not products:
            products = trip.get("fares", [])

        segments_data = trip.get("segments", trip.get("flights", []))
        parsed_segments = self._parse_segments(segments_data)

        for prod in products:
            miles = prod.get("miles", prod.get("price", prod.get("totalPrice", 0)))
            miles = int(miles) if miles else 0
            taxes = float(
                prod.get("taxes", prod.get("taxesAndFees", prod.get("cashPrice", 0)))
            )
            cabin_str = prod.get("cabin", prod.get("cabinType", "economy"))
            cabin = CabinClass.from_united_code(cabin_str)
            seats = prod.get("seatsRemaining", prod.get("availableSeats", 1))
            fare_class = prod.get("fareClass", prod.get("bookingCode", ""))

            if miles == 0:
                continue

            segments = [
                FlightSegment(
                    airline=s.airline,
                    flight_number=s.flight_number,
                    departure_airport=s.departure_airport,
                    arrival_airport=s.arrival_airport,
                    departure_time=s.departure_time,
                    arrival_time=s.arrival_time,
                    duration_minutes=s.duration_minutes,
                    aircraft=s.aircraft,
                    fare_class=fare_class,
                    seats_available=(
                        seats if s == parsed_segments[-1] else None
                    ),
                )
                for s in parsed_segments
            ]

            total_duration = (
                sum(s.duration_minutes for s in parsed_segments)
                if parsed_segments
                else 0
            )
            stops = len(parsed_segments) - 1 if parsed_segments else 0

            offer = AwardOffer(
                source_airline=Airline.UNITED.value,
                query_origin=query.origin.upper(),
                query_destination=query.destination.upper(),
                depart_date=depart_date_str,
                return_date=query.return_date.isoformat() if query.return_date else None,
                segments=segments,
                total_duration_minutes=total_duration,
                stops=stops,
                miles_required=miles,
                taxes_fees=taxes,
                cabin=cabin,
                total_seats_available=seats,
                raw_data=prod,
            )
            results.append(offer)

        return results

    def _parse_bound(
        self, bound: dict[str, Any], query: SearchQuery, depart_date_str: str
    ) -> list[AwardOffer]:
        results: list[AwardOffer] = []
        fares = bound.get("fares", [])
        segments_data = bound.get("segments", [])
        parsed_segments = self._parse_segments(segments_data)

        for fare in fares:
            miles = int(fare.get("miles", fare.get("awardMiles", 0)))
            taxes = float(
                fare.get("taxes", fare.get("cashAmount", 0))
            )
            cabin_str = fare.get("cabin", fare.get("cabinType", "economy"))
            cabin = CabinClass.from_united_code(cabin_str)
            seats = int(fare.get("seatsRemaining", fare.get("availableSeats", 0)))
            fare_class = fare.get("fareClass", fare.get("bookingCode", ""))

            if miles == 0:
                continue

            segments = [
                FlightSegment(
                    airline=s.airline,
                    flight_number=s.flight_number,
                    departure_airport=s.departure_airport,
                    arrival_airport=s.arrival_airport,
                    departure_time=s.departure_time,
                    arrival_time=s.arrival_time,
                    duration_minutes=s.duration_minutes,
                    aircraft=s.aircraft,
                    fare_class=fare_class,
                    seats_available=(
                        seats if s == parsed_segments[-1] else None
                    ),
                )
                for s in parsed_segments
            ]

            total_duration = (
                sum(s.duration_minutes for s in parsed_segments)
                if parsed_segments
                else 0
            )
            stops = len(parsed_segments) - 1 if parsed_segments else 0

            offer = AwardOffer(
                source_airline=Airline.UNITED.value,
                query_origin=query.origin.upper(),
                query_destination=query.destination.upper(),
                depart_date=depart_date_str,
                return_date=query.return_date.isoformat() if query.return_date else None,
                segments=segments,
                total_duration_minutes=total_duration,
                stops=stops,
                miles_required=miles,
                taxes_fees=taxes,
                cabin=cabin,
                total_seats_available=seats,
                raw_data=fare,
            )
            results.append(offer)

        return results

    @staticmethod
    def _parse_segments(segments_data: list[dict[str, Any]]) -> list[FlightSegment]:
        segments: list[FlightSegment] = []
        for seg in segments_data:
            segments.append(
                FlightSegment(
                    airline=seg.get("airline", seg.get("carrier", "")),
                    flight_number=str(
                        seg.get("flightNumber", seg.get("flightNum", ""))
                    ),
                    departure_airport=seg.get(
                        "origin", seg.get("departureAirport", {})
                    ).get("code", "")
                    if isinstance(seg.get("origin"), dict)
                    else seg.get("origin", seg.get("departureAirport", "")),
                    arrival_airport=seg.get(
                        "destination", seg.get("arrivalAirport", {})
                    ).get("code", "")
                    if isinstance(seg.get("destination"), dict)
                    else seg.get("destination", seg.get("arrivalAirport", "")),
                    departure_time=seg.get(
                        "departureTime",
                        seg.get("departTime", seg.get("departDateTime", "")),
                    ),
                    arrival_time=seg.get(
                        "arrivalTime",
                        seg.get("arriveTime", seg.get("arrivalDateTime", "")),
                    ),
                    duration_minutes=int(
                        seg.get("duration", seg.get("flightTime", 0))
                    ),
                    aircraft=seg.get("aircraft", seg.get("plane", "")),
                    fare_class=seg.get("fareClass", seg.get("bookingCode", "")),
                    seats_available=seg.get("seatsRemaining"),
                )
            )
        return segments

    # --- Utility ---

    async def search_route_range(
        self,
        origin: str,
        destination: str,
        start_date: date,
        end_date: date,
        cabin: CabinClass = CabinClass.ECONOMY,
    ) -> dict[str, list[AwardOffer]]:
        """Search a range of dates. Returns dict of date -> offers."""
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
