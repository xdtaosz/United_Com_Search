"""Database repository for award flight data."""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import date, datetime
from pathlib import Path
from typing import Any, Optional

from award_scout.config import settings
from award_scout.db.schema import ALL_SCHEMA_SQL
from award_scout.models import (
    AwardOffer,
    CabinClass,
    FlightSegment,
    SearchQuery,
    WatchRule,
)


class Database:
    """SQLite-based storage for searches, offers, and watch rules."""

    _instance: Optional["Database"] = None
    _lock = threading.Lock()

    def __init__(self, db_path: Optional[Path] = None):
        self.db_path = db_path or settings.db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()

    @property
    def conn(self) -> sqlite3.Connection:
        """Thread-local connection."""
        if not hasattr(self._local, "conn") or self._local.conn is None:
            self._local.conn = sqlite3.connect(str(self.db_path))
            self._local.conn.row_factory = sqlite3.Row
            self._local.conn.execute("PRAGMA journal_mode=WAL")
            self._local.conn.execute("PRAGMA foreign_keys=ON")
        return self._local.conn

    @classmethod
    def get_instance(cls) -> "Database":
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    def init_db(self) -> None:
        """Create all tables and indexes."""
        for stmt in ALL_SCHEMA_SQL:
            self.conn.executescript(stmt)
        self.conn.commit()

    # --- Searches ---

    def save_search(self, query: SearchQuery) -> str:
        import uuid

        search_id = uuid.uuid4().hex[:12]
        airlines = ",".join(query.airlines) if query.airlines else ""
        self.conn.execute(
            """INSERT INTO searches (id, origin, destination, depart_date, return_date,
               cabin, passengers, airlines, created_at, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                search_id,
                query.origin.upper(),
                query.destination.upper(),
                query.depart_date.isoformat(),
                query.return_date.isoformat() if query.return_date else None,
                query.cabin.value,
                query.passengers,
                airlines,
                datetime.utcnow().isoformat(),
                "completed",
            ),
        )
        self.conn.commit()
        return search_id

    # --- Offers ---

    def save_offers(self, search_id: str, offers: list[AwardOffer]) -> int:
        count = 0
        for offer in offers:
            segments_json = json.dumps(
                [
                    {
                        "airline": s.airline,
                        "flight_number": s.flight_number,
                        "departure_airport": s.departure_airport,
                        "arrival_airport": s.arrival_airport,
                        "departure_time": s.departure_time,
                        "arrival_time": s.arrival_time,
                        "duration_minutes": s.duration_minutes,
                        "aircraft": s.aircraft,
                        "fare_class": s.fare_class,
                        "seats_available": s.seats_available,
                    }
                    for s in offer.segments
                ],
                ensure_ascii=False,
            )
            self.conn.execute(
                """INSERT OR REPLACE INTO award_offers
                   (id, search_id, source_airline, query_origin, query_destination,
                    depart_date, return_date, stops, total_duration_minutes,
                    miles_required, taxes_fees, cabin, total_seats_available,
                    segments_json, booking_link, scraped_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    offer.id,
                    search_id,
                    offer.source_airline,
                    offer.query_origin,
                    offer.query_destination,
                    offer.depart_date,
                    offer.return_date,
                    offer.stops,
                    offer.total_duration_minutes,
                    offer.miles_required,
                    offer.taxes_fees,
                    offer.cabin.value,
                    offer.total_seats_available,
                    segments_json,
                    offer.booking_link,
                    offer.scraped_at,
                ),
            )
            count += 1
        self.conn.commit()
        return count

    def query_offers(
        self,
        origin: Optional[str] = None,
        destination: Optional[str] = None,
        depart_date: Optional[str] = None,
        cabin: Optional[str] = None,
        max_miles: Optional[int] = None,
        max_stops: Optional[int] = None,
        airlines: Optional[list[str]] = None,
        limit: int = 100,
    ) -> list[AwardOffer]:
        clauses: list[str] = ["1=1"]
        params: list[Any] = []

        if origin:
            clauses.append("query_origin = ?")
            params.append(origin.upper())
        if destination:
            clauses.append("query_destination = ?")
            params.append(destination.upper())
        if depart_date:
            clauses.append("depart_date = ?")
            params.append(depart_date)
        if cabin:
            clauses.append("cabin = ?")
            params.append(cabin)
        if max_miles is not None:
            clauses.append("miles_required <= ?")
            params.append(max_miles)
        if max_stops is not None:
            clauses.append("stops <= ?")
            params.append(max_stops)
        if airlines:
            placeholders = ",".join("?" for _ in airlines)
            clauses.append(f"source_airline IN ({placeholders})")
            params.extend(airlines)

        sql = f"SELECT * FROM award_offers WHERE {' AND '.join(clauses)} ORDER BY miles_required ASC LIMIT ?"
        params.append(limit)

        rows = self.conn.execute(sql, params).fetchall()
        return [self._row_to_offer(r) for r in rows]

    def get_recent_offers(
        self, origin: str, destination: str, max_days: int = 7
    ) -> list[AwardOffer]:
        cutoff = (  # noqa: F841
            date.today().isoformat()
        )
        rows = self.conn.execute(
            """SELECT * FROM award_offers
               WHERE query_origin = ? AND query_destination = ?
               ORDER BY scraped_at DESC LIMIT ?""",
            (origin.upper(), destination.upper(), max_days * 20),
        ).fetchall()
        return [self._row_to_offer(r) for r in rows]

    @staticmethod
    def _row_to_offer(row: sqlite3.Row) -> AwardOffer:
        segments_data = json.loads(row["segments_json"]) if row["segments_json"] else []
        segments = [
            FlightSegment(
                airline=s.get("airline", ""),
                flight_number=s.get("flight_number", ""),
                departure_airport=s.get("departure_airport", ""),
                arrival_airport=s.get("arrival_airport", ""),
                departure_time=s.get("departure_time", ""),
                arrival_time=s.get("arrival_time", ""),
                duration_minutes=s.get("duration_minutes", 0),
                aircraft=s.get("aircraft"),
                fare_class=s.get("fare_class"),
                seats_available=s.get("seats_available"),
            )
            for s in segments_data
        ]
        return AwardOffer(
            id=row["id"],
            source_airline=row["source_airline"],
            query_origin=row["query_origin"],
            query_destination=row["query_destination"],
            depart_date=row["depart_date"],
            return_date=row["return_date"],
            segments=segments,
            total_duration_minutes=row["total_duration_minutes"],
            stops=row["stops"],
            miles_required=row["miles_required"],
            taxes_fees=row["taxes_fees"],
            cabin=CabinClass(row["cabin"]),
            total_seats_available=row["total_seats_available"],
            booking_link=row["booking_link"],
            scraped_at=row["scraped_at"],
        )

    # --- Watch Rules ---

    def save_watch(self, rule: WatchRule) -> str:
        airlines = ",".join(rule.airlines) if rule.airlines else ""
        self.conn.execute(
            """INSERT OR REPLACE INTO watch_rules
               (id, origin, destination, start_date, end_date, cabin,
                max_miles, max_stops, airlines, active, notify_via,
                notify_target, created_at, last_checked)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                rule.id,
                rule.origin.upper(),
                rule.destination.upper(),
                rule.start_date.isoformat() if rule.start_date else None,
                rule.end_date.isoformat() if rule.end_date else None,
                rule.cabin.value if rule.cabin else None,
                rule.max_miles,
                rule.max_stops,
                airlines,
                1 if rule.active else 0,
                rule.notify_via,
                rule.notify_target,
                rule.created_at,
                rule.last_checked,
            ),
        )
        self.conn.commit()
        return rule.id

    def get_active_watches(self) -> list[WatchRule]:
        rows = self.conn.execute(
            "SELECT * FROM watch_rules WHERE active = 1"
        ).fetchall()
        return [self._row_to_watch(r) for r in rows]

    def get_all_watches(self) -> list[WatchRule]:
        rows = self.conn.execute(
            "SELECT * FROM watch_rules ORDER BY created_at DESC"
        ).fetchall()
        return [self._row_to_watch(r) for r in rows]

    def update_watch_checked(self, watch_id: str) -> None:
        self.conn.execute(
            "UPDATE watch_rules SET last_checked = ? WHERE id = ?",
            (datetime.utcnow().isoformat(), watch_id),
        )
        self.conn.commit()

    def deactivate_watch(self, watch_id: str) -> None:
        self.conn.execute(
            "UPDATE watch_rules SET active = 0 WHERE id = ?", (watch_id,)
        )
        self.conn.commit()

    def delete_watch(self, watch_id: str) -> None:
        self.conn.execute("DELETE FROM watch_rules WHERE id = ?", (watch_id,))
        self.conn.commit()

    @staticmethod
    def _row_to_watch(row: sqlite3.Row) -> WatchRule:
        return WatchRule(
            id=row["id"],
            origin=row["origin"],
            destination=row["destination"],
            start_date=(
                date.fromisoformat(row["start_date"]) if row["start_date"] else None
            ),
            end_date=(
                date.fromisoformat(row["end_date"]) if row["end_date"] else None
            ),
            cabin=CabinClass(row["cabin"]) if row["cabin"] else None,
            max_miles=row["max_miles"],
            max_stops=row["max_stops"],
            airlines=row["airlines"].split(",") if row["airlines"] else None,
            active=bool(row["active"]),
            notify_via=row["notify_via"],
            notify_target=row["notify_target"],
            created_at=row["created_at"],
            last_checked=row["last_checked"],
        )
