from __future__ import annotations

import asyncio
import json
import re
from typing import Any, Optional

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

AA_BASE = "https://www.aa.com"
AA_LOGIN = f"{AA_BASE}/home.do?anchorEvent=false#/"
AA_AWARD_SEARCH = f"{AA_BASE}/booking/find-flights"


class AmericanScraper(BaseAirlineScraper):
    def __init__(self):
        super().__init__(Airline.AMERICAN.value)

    @property
    def login_url(self) -> str:
        return AA_LOGIN

    # --- Login ---

    async def _is_logged_in(self, page: Page) -> bool:
        try:
            await page.goto(AA_BASE, wait_until="domcontentloaded")
            await asyncio.sleep(2)
            content = await page.content()
            return "Hi, " in content or "aadvantage" in content.lower() or "sign out" in content.lower()
        except Exception:
            return False

    async def _do_login(self) -> bool:
        ctx = await self._ensure_browser()
        page = await ctx.new_page()
        page.set_default_timeout(settings.browser_timeout_ms)

        try:
            await page.goto(AA_LOGIN, wait_until="networkidle")
            await asyncio.sleep(3)

            aa_number = settings.aa_number
            password = settings.aa_password
            if not aa_number or not password:
                raise LoginError("AA_NUMBER and AA_PASSWORD must be set in .env")

            # AA's login may redirect to a login portal
            current_url = page.url
            if "login" not in current_url.lower() and "aa.com" in current_url:
                # Already on aa.com, find the login button
                try:
                    login_btn = await page.wait_for_selector(
                        'a[href*="login"], button[data-test="loginButton"]',
                        timeout=5000,
                    )
                    if login_btn:
                        await login_btn.click()
                        await asyncio.sleep(3)
                except Exception:
                    pass

            # Fill credentials
            user_field = await page.wait_for_selector(
                'input[name="loginfmt"], input[id="userId"], input[name="userId"], input[data-test="aadvantageNumber"]',
                timeout=15000,
            )
            if user_field:
                await user_field.fill(aa_number)
            else:
                raise LoginError("Could not find AA login field")

            pw_field = await page.wait_for_selector(
                'input[name="passwd"], input[id="password"], input[name="password"], input[data-test="password"]',
                timeout=5000,
            )
            if pw_field:
                await pw_field.fill(password)

            submit_btn = await page.wait_for_selector(
                'button[type="submit"], input[type="submit"], button[data-test="signInButton"]',
                timeout=5000,
            )
            if submit_btn:
                await submit_btn.click()

            await asyncio.sleep(5)

            # Handle MFA
            if await self._detect_mfa(page):
                code = await self._handle_mfa(page)
                if code:
                    await self._submit_mfa(page, code)
                    await asyncio.sleep(3)

            if not await self._is_logged_in(page):
                raise LoginError("AA login failed")

            await self.save_cookies()
            return True

        finally:
            await page.close()

    async def _detect_mfa(self, page: Page) -> bool:
        content = await page.content()
        indicators = [
            "verification code",
            "two-factor",
            "multi-factor",
            "send code",
            "verify your identity",
            "security code",
            "mfa",
        ]
        return any(indicator in content.lower() for indicator in indicators)

    async def _handle_mfa(self, page: Page) -> Optional[str]:
        if self._mfa_callback:
            return await self._mfa_callback()
        return None

    async def _submit_mfa(self, page: Page, code: str) -> None:
        try:
            mfa_input = await page.wait_for_selector(
                'input[name="otc"], input[autocomplete="one-time-code"], input[data-test="otpInput"]',
                timeout=10000,
            )
            if mfa_input:
                await mfa_input.fill(code)
                submit = await page.wait_for_selector(
                    'button[type="submit"]:not([disabled])', timeout=5000
                )
                if submit:
                    await submit.click()
                    await asyncio.sleep(2)
        except Exception:
            pass

    # --- Search ---

    async def search(self, query: SearchQuery) -> list[AwardOffer]:
        if not await self.login():
            raise LoginError("Cannot search without successful login")

        ctx = await self._ensure_browser()
        page = await ctx.new_page()
        page.set_default_timeout(settings.browser_timeout_ms)

        api_responses: list[dict[str, Any]] = []

        async def capture_api(response):
            url = response.url
            if ("award" in url.lower() or "shopping" in url.lower()) and response.status == 200:
                try:
                    body = await response.json()
                    if "flights" in body or "trips" in body or "results" in body:
                        api_responses.append(body)
                except Exception:
                    pass

        page.on("response", capture_api)

        try:
            search_url = self._build_search_url(query)
            await page.goto(search_url, wait_until="networkidle")
            await asyncio.sleep(5)

            # Click search button if needed
            try:
                search_btn = await page.wait_for_selector(
                    'button[type="submit"], button[data-test="searchButton"]',
                    timeout=5000,
                )
                if search_btn:
                    await search_btn.click()
                    await asyncio.sleep(5)
            except Exception:
                pass

            offers: list[AwardOffer] = []
            for resp in api_responses:
                offers.extend(self._parse_response(resp, query))

            return offers

        finally:
            page.remove_listener("response", capture_api)
            await page.close()

    def _build_search_url(self, query: SearchQuery) -> str:
        base = f"{AA_AWARD_SEARCH}?type=Award"
        params = (
            f"&origin={query.origin.upper()}"
            f"&destination={query.destination.upper()}"
            f"&departDate={query.depart_date.isoformat()}"
        )
        if query.return_date:
            params += f"&returnDate={query.return_date.isoformat()}"
        params += f"&passengers={query.passengers}"
        if query.cabin == CabinClass.BUSINESS:
            params += "&cabin=business"
        elif query.cabin == CabinClass.FIRST:
            params += "&cabin=first"
        return base + params

    def _parse_response(
        self, data: dict[str, Any], query: SearchQuery
    ) -> list[AwardOffer]:
        offers: list[AwardOffer] = []

        trips = (
            data.get("trips", [])
            or data.get("data", {}).get("trips", [])
            or data.get("results", [])
        )

        for trip in trips:
            depart_date_str = (
                trip.get("departDate", "")
                or trip.get("date", "")
                or query.depart_date.isoformat()
            )
            bounds = trip.get("bounds", trip.get("flights", [trip]))
            for bound in bounds:
                fares = bound.get("fares", bound.get("products", [{}]))
                segments_data = bound.get("segments", bound.get("flights", []))
                parsed_segments = AmericanScraper._parse_segments(segments_data)

                for fare in fares:
                    miles = int(fare.get("miles", fare.get("awardMiles", fare.get("totalPrice", 0))))
                    taxes = float(
                        fare.get("taxes", fare.get("taxesAndFees", fare.get("cashAmount", 0.0)))
                    )
                    cabin_str = fare.get("cabin", fare.get("cabinType", "coach"))
                    cabin = CabinClass.from_aa_code(cabin_str)
                    seats = int(fare.get("seatsRemaining", fare.get("availableSeats", 1)))
                    fare_class = fare.get("fareClass", fare.get("bookingCode", ""))

                    if miles == 0 and taxes == 0:
                        continue

                    segments = [
                        FlightSegment(
                            airline=s.airline or "AA",
                            flight_number=s.flight_number,
                            departure_airport=s.departure_airport,
                            arrival_airport=s.arrival_airport,
                            departure_time=s.departure_time,
                            arrival_time=s.arrival_time,
                            duration_minutes=s.duration_minutes,
                            aircraft=s.aircraft,
                            fare_class=fare_class,
                            seats_available=seats if s == parsed_segments[-1] else None,
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
                        source_airline=Airline.AMERICAN.value,
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
                    offers.append(offer)

        return offers

    @staticmethod
    def _parse_segments(segments_data: list[dict[str, Any]]) -> list[FlightSegment]:
        segments: list[FlightSegment] = []
        for seg in segments_data:
            dep = seg.get("departure", seg.get("origin", {}))
            arr = seg.get("arrival", seg.get("destination", {}))
            dep_code = dep.get("code", dep.get("airportCode", dep if isinstance(dep, str) else ""))
            arr_code = arr.get("code", arr.get("airportCode", arr if isinstance(arr, str) else ""))

            segments.append(
                FlightSegment(
                    airline=seg.get("airline", seg.get("carrier", "AA")),
                    flight_number=str(
                        seg.get("flightNumber", seg.get("flightNum", ""))
                    ),
                    departure_airport=dep_code,
                    arrival_airport=arr_code,
                    departure_time=seg.get(
                        "departureTime",
                        seg.get("departTime", seg.get("departDateTime", "")),
                    ),
                    arrival_time=seg.get(
                        "arrivalTime",
                        seg.get("arriveTime", seg.get("arrivalDateTime", "")),
                    ),
                    duration_minutes=int(
                        seg.get("duration", seg.get("flightTime", seg.get("elapsedTime", 0)))
                    ),
                    aircraft=seg.get("aircraft", seg.get("plane", "")),
                    fare_class=seg.get("fareClass", seg.get("bookingCode", "")),
                    seats_available=seg.get("seatsRemaining", seg.get("availableSeats")),
                )
            )
        return segments
