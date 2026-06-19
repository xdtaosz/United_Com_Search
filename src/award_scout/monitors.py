"""Watch-based monitoring system for award flight price alerts."""

from __future__ import annotations

import asyncio
import random
import smtplib
import ssl
from datetime import date, timedelta
from email.mime.text import MIMEText

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
    """Check one watch rule: calendar pre-filter → per-day FetchFlights with delays."""
    start = rule.start_date or date.today()
    end = rule.end_date or date.today()
    airlines = rule.airlines or ["united"]

    all_offers: list[AwardOffer] = []
    first_airline = True

    for airline in airlines:
        try:
            if airline == "united":
                from award_scout.scrapers.united import UnitedScraper

                async with UnitedScraper() as scraper:
                    await scraper.login()
                    offers = await scraper.search_range(
                        origin=rule.origin,
                        destination=rule.destination,
                        start_date=start,
                        end_date=end,
                        cabin=rule.cabin or CabinClass.ECONOMY,
                        max_miles=rule.max_miles,
                    )
                    all_offers.extend(offers)
            else:
                # Other airlines: fallback to single-date search with delay
                current = start
                while current <= end:
                    query = SearchQuery(
                        origin=rule.origin,
                        destination=rule.destination,
                        depart_date=current,
                        cabin=rule.cabin or CabinClass.ECONOMY,
                        max_miles=rule.max_miles,
                    )
                    offers = await _search_airline(airline, query)
                    all_offers.extend(offers)
                    current += timedelta(days=1)
        except Exception:
            pass

        if not first_airline and len(airlines) > 1:
            base = settings.search_delay_seconds
            jitter = random.uniform(0, base * 0.5)
            await asyncio.sleep(base + jitter)
        first_airline = False

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
    EXCLUDE_AIRPORTS = {"MNL"}  # Exclude Manila connections
    matched = offers

    if rule.max_miles is not None:
        matched = [o for o in matched if o.miles_required <= rule.max_miles]
    if rule.max_stops is not None:
        matched = [o for o in matched if o.stops <= rule.max_stops]
    if rule.cabin:
        matched = [o for o in matched if o.cabin == rule.cabin]
    # Exclude flights transiting through blocked airports
    matched = [o for o in matched if not any(
        s.departure_airport in EXCLUDE_AIRPORTS or s.arrival_airport in EXCLUDE_AIRPORTS
        for s in o.segments
    )]

    return sorted(matched, key=lambda o: o.miles_required)


async def _send_notification(rule: WatchRule, offers: list[AwardOffer]) -> None:
    """Send notification about matching offers."""
    if rule.notify_via == "ntfy":
        await _send_ntfy(rule, offers)
    elif rule.notify_via == "email":
        _send_email(rule, offers)
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


def _send_email(rule: WatchRule, offers: list[AwardOffer]) -> None:
    to_addr = rule.notify_target or settings.email_to
    smtp_user = settings.email_smtp_user or settings.email_from
    if not to_addr or not smtp_user or not settings.email_smtp_password:
        print("  Email not configured (set EMAIL_TO, EMAIL_SMTP_USER, EMAIL_SMTP_PASSWORD in .env)")
        return

    subject = f"Award Alert: {rule.origin} → {rule.destination} — {len(offers)} deal(s) found"

    rows = ""
    for o in offers:
        first_seg = o.segments[0] if o.segments else None
        last_seg = o.segments[-1] if o.segments else None
        flight = f"{first_seg.flight_number}" if first_seg else ""
        dep = first_seg.departure_time[:5] if first_seg and first_seg.departure_time else ""
        arr = last_seg.arrival_time[:5] if last_seg and last_seg.arrival_time else ""
        flight_col = f"{flight} {dep}–{arr}" if flight else "—"
        duration = f"{o.total_duration_minutes // 60}h{o.total_duration_minutes % 60}m" if o.total_duration_minutes else "—"

        rows += f"""
        <tr>
            <td style="padding:8px;border-bottom:1px solid #ddd;text-align:center;">{o.depart_date}</td>
            <td style="padding:8px;border-bottom:1px solid #ddd;text-align:center;">{o.stops}</td>
            <td style="padding:8px;border-bottom:1px solid #ddd;text-align:center;">{duration}</td>
            <td style="padding:8px;border-bottom:1px solid #ddd;text-align:center;">{o.cabin.value.title()}</td>
            <td style="padding:8px;border-bottom:1px solid #ddd;text-align:center;font-weight:bold;color:#e53935;">{o.miles_required:,}</td>
            <td style="padding:8px;border-bottom:1px solid #ddd;text-align:center;">${o.taxes_fees:.2f}</td>
            <td style="padding:8px;border-bottom:1px solid #ddd;text-align:center;">{o.total_seats_available}</td>
            <td style="padding:8px;border-bottom:1px solid #ddd;text-align:center;">{flight_col}</td>
        </tr>"""

    html = f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"></head>
<body style="font-family:Arial,sans-serif;background:#f5f5f5;padding:20px;">
<div style="max-width:800px;margin:0 auto;background:#fff;border-radius:8px;overflow:hidden;box-shadow:0 2px 8px rgba(0,0,0,.1);">
<div style="background:#1a237e;color:#fff;padding:20px;text-align:center;">
<h2 style="margin:0;">✈️ Award Alert</h2>
<p style="margin:8px 0 0;opacity:.9;">{rule.origin} → {rule.destination}</p>
</div>
<div style="padding:20px;">
<p><strong>{len(offers)} matching award offer(s)</strong> found within your criteria.</p>
<table style="width:100%;border-collapse:collapse;margin-top:12px;">
<thead>
<tr style="background:#f5f5f5;">
<th style="padding:8px;border-bottom:2px solid #ddd;text-align:center;">Date</th>
<th style="padding:8px;border-bottom:2px solid #ddd;text-align:center;">Stops</th>
<th style="padding:8px;border-bottom:2px solid #ddd;text-align:center;">Duration</th>
<th style="padding:8px;border-bottom:2px solid #ddd;text-align:center;">Cabin</th>
<th style="padding:8px;border-bottom:2px solid #ddd;text-align:center;">Miles</th>
<th style="padding:8px;border-bottom:2px solid #ddd;text-align:center;">Taxes</th>
<th style="padding:8px;border-bottom:2px solid #ddd;text-align:center;">Seats</th>
<th style="padding:8px;border-bottom:2px solid #ddd;text-align:center;">Flight</th>
</tr>
</thead>
<tbody>{rows}
</tbody>
</table>
<p style="margin-top:16px;font-size:12px;color:#888;">Sent by <strong>award-scout</strong> — check again next hour for new deals.</p>
</div>
</div>
</body>
</html>"""

    msg = MIMEText(html, "html")
    msg["Subject"] = subject
    msg["From"] = settings.email_from or smtp_user
    msg["To"] = to_addr

    try:
        context = ssl.create_default_context()
        with smtplib.SMTP_SSL(
            settings.email_smtp_host, settings.email_smtp_port, context=context, timeout=30
        ) as server:
            server.login(smtp_user, settings.email_smtp_password)
            to_addrs = [a.strip() for a in to_addr.split(",")]
            server.sendmail(msg["From"], to_addrs, msg.as_string())
        print(f"  Email sent to {to_addr}")
    except Exception as e:
        print(f"  Failed to send email: {e}")
