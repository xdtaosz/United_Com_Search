"""Data models for award flight search."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum
from typing import Any, Optional


class CabinClass(str, Enum):
    ECONOMY = "economy"
    PREMIUM_ECONOMY = "premium_economy"
    BUSINESS = "business"
    FIRST = "first"

    @classmethod
    def from_united_code(cls, code: str) -> "CabinClass":
        mapping = {
            "economy": cls.ECONOMY,
            "premium": cls.PREMIUM_ECONOMY,
            "business": cls.BUSINESS,
            "first": cls.FIRST,
        }
        return mapping.get(code.lower(), cls.ECONOMY)

    @classmethod
    def from_aa_code(cls, code: str) -> "CabinClass":
        mapping = {
            "coach": cls.ECONOMY,
            "premium": cls.PREMIUM_ECONOMY,
            "business": cls.BUSINESS,
            "first": cls.FIRST,
        }
        return mapping.get(code.lower(), cls.ECONOMY)


class Airline(str, Enum):
    UNITED = "united"
    AMERICAN = "american"
    DELTA = "delta"
    ALASKA = "alaska"
    JETBLUE = "jetblue"
    SOUTHWEST = "southwest"


@dataclass
class FlightSegment:
    airline: str
    flight_number: str
    departure_airport: str
    arrival_airport: str
    departure_time: str  # ISO format
    arrival_time: str  # ISO format
    duration_minutes: int
    aircraft: Optional[str] = None
    fare_class: Optional[str] = None  # e.g. "I", "X", "O"
    seats_available: Optional[int] = None


@dataclass
class AwardOffer:
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    source_airline: str = ""
    query_origin: str = ""
    query_destination: str = ""
    depart_date: str = ""
    return_date: Optional[str] = None

    segments: list[FlightSegment] = field(default_factory=list)
    total_duration_minutes: int = 0
    stops: int = 0

    miles_required: int = 0
    taxes_fees: float = 0.0
    cabin: CabinClass = CabinClass.ECONOMY
    total_seats_available: int = 0

    booking_link: Optional[str] = None
    raw_data: dict[str, Any] | None = None
    scraped_at: str = field(default_factory=lambda: datetime.utcnow().isoformat())


@dataclass
class SearchQuery:
    origin: str  # IATA code
    destination: str  # IATA code
    depart_date: date
    return_date: Optional[date] = None
    cabin: CabinClass = CabinClass.ECONOMY
    passengers: int = 1
    max_stops: Optional[int] = None
    max_miles: Optional[int] = None
    airlines: Optional[list[str]] = None  # which programs to search


@dataclass
class WatchRule:
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    origin: str = ""
    destination: str = ""
    start_date: Optional[date] = None
    end_date: Optional[date] = None
    cabin: Optional[CabinClass] = None
    max_miles: Optional[int] = None
    max_stops: Optional[int] = None
    airlines: Optional[list[str]] = None
    active: bool = True
    notify_via: Optional[str] = None  # "ntfy", "email"
    notify_target: Optional[str] = None
    created_at: str = field(default_factory=lambda: datetime.utcnow().isoformat())
    last_checked: Optional[str] = None
