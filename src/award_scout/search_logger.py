"""Structured search log writer for stage-1 (calendar) and stage-2 (flights)."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from award_scout.config import settings


class SearchLogger:
    """Writes two-stage search results to ~/.award_scout/logs/search.log."""

    def __init__(self, log_path: Path | None = None):
        self._path = log_path or (settings.logs_dir / "search.log")
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def _write(self, text: str) -> None:
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        with open(self._path, "a") as f:
            f.write(f"{ts} {text}\n")

    # --- Stage 1: Calendar ---

    def stage1_start(self, route: str, cabin: str, date_range: str) -> None:
        self._write(f"[STAGE1] {route} {cabin} | range={date_range} | checking calendar...")

    def stage1_hit(self, d: str, miles: int, cash: float) -> None:
        self._write(f"[STAGE1]   ✓ {d} | {miles:,}mi + ${cash:.2f}")

    def stage1_miss(self, d: str, miles: int | None = None) -> None:
        if miles:
            self._write(f"[STAGE1]   ✗ {d} | {miles:,}mi (over limit)")
        else:
            self._write(f"[STAGE1]   ✗ {d} | no availability")

    def stage1_summary(self, hits: int, total: int) -> None:
        self._write(f"[STAGE1] summary: {hits}/{total} days qualify")

    # --- Stage 2: Flights ---

    def stage2_start(self, d: str) -> None:
        self._write(f"[STAGE2] {d} | fetching detailed flights...")

    def stage2_flight(
        self,
        flight_num: str,
        route: str,
        times: str,
        duration: str,
        stops: int,
        miles: int,
        cash: float,
        cabin: str,
        seats: int | str,
        fare_class: str,
    ) -> None:
        stops_str = "nonstop" if stops == 0 else f"{stops} stop(s)"
        self._write(
            f"[STAGE2]   {flight_num} {route} {times} ({duration}) | "
            f"{miles:,}mi + ${cash:.2f} | {cabin} | {seats} seats | "
            f"{fare_class} | {stops_str}"
        )

    def stage2_empty(self, d: str) -> None:
        self._write(f"[STAGE2]   (no flights found)")

    def stage2_summary(self, d: str, count: int) -> None:
        self._write(f"[STAGE2] {d}: {count} flight(s) found")

    # --- Error ---

    def error(self, context: str, detail: str = "") -> None:
        msg = f"[ERROR] {context}"
        if detail:
            msg += f" | {detail}"
        self._write(msg)
