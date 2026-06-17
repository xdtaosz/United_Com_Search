"""Watch-based monitoring system for award flight price alerts."""

from __future__ import annotations

import asyncio
import json
from datetime import date, datetime
from typing import Optional

import httpx

from award_scout.config import settings
from award_scout.db.repository import Database
from award_scout.models import AwardOffer, CabinClass, SearchQuery, WatchRule


async def run_watches() -> None:
    """Execute all active watch rules and notify on matches."""
    db = Database.get_instance()
    db.init_db()
    rules = db.get_active_watches()

    if not rules:
        print("No active watch rules.")
        return

    for rule in rules:
        print(f"Checking watch: {rule.origin} → {rule.destination} ...")
        offers = await _check_rule(rule)
        db.update_watch_checked(rule.id)

        if offers:
            matched = _match_rule(offers, rule)
            if matched:
                db.save_offers(f"watch_{rule.id}", matched)
                await _send_notification(rule, matched)
                print(f"  → {len(matched)} matching offer(s) found, notification sent")
            else:
                print(f"  → {len(offers)} offers found but none matched criteria")
        else:
            print(f"  → No offers found")


async def _check_rule(rule: WatchRule) -> list[AwardOffer]:
    """Run a search for a watch rule."""
    start = rule.start_date or date.today()
    end = rule.end_date or date.today()

    all_offers: list[AwardOffer] = []
    current = start

    while current <= end:
        query = SearchQuery(
            origin=rule.origin,
            destination=rule.destination,
            depart_date=current,
            cabin=rule.cabin or CabinClass.ECONOMY,
            airlines=rule.airlines,
            max_miles=rule.max_miles,
            max_stops=rule.max_stops,
        )

        for airline in rule.airlines or ["united", "american"]:
            try:
                offers = await _search_airline(airline, query)
                all_offers.extend(offers)
            except Exception:
                pass

        from datetime import timedelta
        current += timedelta(days=1)

    return all_offers


async def _search_airline(airline: str, query: SearchQuery) -> list[AwardOffer]:
    """Search a specific airline for award offers."""
    if airline == "united":
        from award_scout.scrapers.united import UnitedScraper

        async with UnitedScraper() as scraper:
            return await scraper.search(query)
    elif airline == "american":
        from award_scout.scrapers.american import AmericanScraper

        async with AmericanScraper() as scraper:
            return await scraper.search(query)
    return []


def _match_rule(offers: list[AwardOffer], rule: WatchRule) -> list[AwardOffer]:
    """Filter offers that match a watch rule's criteria."""
    matched = offers

    if rule.max_miles is not None:
        matched = [o for o in matched if o.miles_required <= rule.max_miles]
    if rule.max_stops is not None:
        matched = [o for o in matched if o.stops <= rule.max_stops]
    if rule.cabin:
        matched = [o for o in matched if o.cabin == rule.cabin]

    return sorted(matched, key=lambda o: o.miles_required)


async def _send_notification(rule: WatchRule, offers: list[AwardOffer]) -> None:
    """Send notification about matching offers."""
    if rule.notify_via == "ntfy":
        await _send_ntfy(rule, offers)
    else:
        # Default: just print
        print(f"\n  ALERT: {len(offers)} matching offers for {rule.origin} → {rule.destination}")
        for o in offers[:5]:
            print(f"    {o.depart_date} | {o.miles_required:,} miles + ${o.taxes_fees:.2f} | {o.cabin.value}")


async def _send_ntfy(rule: WatchRule, offers: list[AwardOffer]) -> None:
    """Send notification via ntfy.sh."""
    topic = rule.notify_target or settings.ntfy_topic
    if not topic:
        return

    title = f"Award Alert: {rule.origin} → {rule.destination}"
    lines = [f"{len(offers)} matching award offers found:"]
    for o in offers[:5]:
        lines.append(f"  {o.depart_date} | {o.miles_required:,} mi + ${o.taxes_fees:.2f} | {o.cabin.value}")
    if len(offers) > 5:
        lines.append(f"  ... and {len(offers) - 5} more")

    message = "\n".join(lines)

    try:
        async with httpx.AsyncClient() as client:
            await client.post(
                f"{settings.ntfy_server}/{topic}",
                content=message,
                headers={"Title": title, "Tags": "airplane"},
                timeout=10,
            )
    except Exception as e:
        print(f"  Failed to send ntfy notification: {e}")
