"""FastAPI web interface for award-scout."""

from __future__ import annotations

import asyncio
import smtplib
import ssl
from datetime import date, datetime, timezone
from email.mime.text import MIMEText
from pathlib import Path
from typing import Annotated, Any, Optional

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse
from jinja2 import Environment, FileSystemLoader

from award_scout.config import settings
from award_scout.models import CabinClass, SearchQuery

TEMPLATES_DIR = Path(__file__).parent / "templates"
app = FastAPI(title="Award Scout")
jinja_env = Environment(loader=FileSystemLoader(str(TEMPLATES_DIR)), autoescape=True)


def _render(template_name: str, **ctx) -> HTMLResponse:
    template = jinja_env.get_template(template_name)
    return HTMLResponse(template.render(**ctx))

CABIN_CHOICES = [
    ("business", "Business"),
    ("economy", "Economy"),
    ("premium_economy", "Premium Economy"),
    ("first", "First"),
]


async def _run_scraper(search_params: dict[str, Any]) -> list[dict[str, Any]] | dict[str, str]:
    """Run the United scraper and return offer dicts."""
    from award_scout.scrapers.united import UnitedScraper

    async with UnitedScraper() as scraper:
        ok = await scraper.login()
        if not ok:
            return {"error": "Login failed. Run 'award-scout login united' first."}

        cabin = CabinClass(search_params.get("cabin", "business"))
        exclude = {
            a.strip().upper()
            for a in search_params.get("exclude_airports", "").split(",")
            if a.strip()
        }

        offers = await scraper.search_range(
            origin=search_params["origin"].upper(),
            destination=search_params["destination"].upper(),
            start_date=search_params["start_date"],
            end_date=search_params["end_date"],
            cabin=cabin,
            max_miles=search_params.get("max_miles"),
        )

        results = []
        for o in offers:
            if exclude and any(
                s.departure_airport in exclude or s.arrival_airport in exclude
                for s in o.segments
            ):
                continue

            segments_list = []
            for s in o.segments:
                dep = s.departure_time[:16] if s.departure_time else "?"
                arr = s.arrival_time[:16] if s.arrival_time else "?"
                segments_list.append(
                    f"{s.airline}{s.flight_number} {s.departure_airport}→{s.arrival_airport} {dep}–{arr}"
                )

            dur_h = o.total_duration_minutes // 60
            dur_m = o.total_duration_minutes % 60

            results.append(
                {
                    "date": o.depart_date,
                    "stops": o.stops,
                    "duration": f"{dur_h}h{dur_m}m",
                    "cabin": o.cabin.value.title(),
                    "miles": o.miles_required,
                    "taxes": o.taxes_fees,
                    "seats": o.total_seats_available,
                    "segments": " | ".join(segments_list),
                    "stops_label": "nonstop" if o.stops == 0 else f"{o.stops} stop(s)",
                }
            )

        results.sort(key=lambda r: r["miles"])
        return results


def _send_results_email(
    to_addr: str, results: list[dict[str, Any]], params: dict[str, Any]
) -> bool:
    """Send results via email. Returns True on success."""
    smtp_user = settings.email_smtp_user or settings.email_from
    if not to_addr or not smtp_user or not settings.email_smtp_password:
        return False

    rows_html = ""
    for r in results:
        rows_html += f"""
        <tr>
            <td style="padding:6px;border-bottom:1px solid #eee;">{r['date']}</td>
            <td style="padding:6px;border-bottom:1px solid #eee;">{r['cabin']}</td>
            <td style="padding:6px;border-bottom:1px solid #eee;">{r['stops_label']}</td>
            <td style="padding:6px;border-bottom:1px solid #eee;font-weight:bold;color:#e53935;">{r['miles']:,}</td>
            <td style="padding:6px;border-bottom:1px solid #eee;">${r['taxes']:.2f}</td>
            <td style="padding:6px;border-bottom:1px solid #eee;">{r['seats']}</td>
            <td style="padding:6px;border-bottom:1px solid #eee;font-size:12px;">{r['segments']}</td>
        </tr>"""

    route = f"{params['origin']} → {params['destination']}"
    html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"></head>
<body style="font-family:Arial;background:#f5f5f5;padding:20px;">
<div style="max-width:800px;margin:0 auto;background:#fff;border-radius:8px;box-shadow:0 2px 8px rgba(0,0,0,.1);">
<div style="background:#1a237e;color:#fff;padding:20px;text-align:center;">
<h2 style="margin:0;">Award Scout Results</h2>
<p style="margin:8px 0 0;">{route} | {params.get('cabin','').title()} | ≤{params['max_miles']:,}mi</p>
</div>
<div style="padding:20px;">
<p>{len(results)} award offers found ({params['start_date']} to {params['end_date']})</p>
<table style="width:100%;border-collapse:collapse;font-size:13px;">
<tr style="background:#f5f5f5;"><th>Date</th><th>Cabin</th><th>Stops</th><th>Miles</th><th>Taxes</th><th>Seats</th><th>Flight</th></tr>
{rows_html}
</table>
<p style="margin-top:16px;font-size:12px;color:#888;">Sent by award-scout</p>
</div></div></body></html>"""

    msg = MIMEText(html, "html")
    msg["Subject"] = f"Award Scout: {route} — {len(results)} results"
    msg["From"] = smtp_user
    msg["To"] = to_addr

    to_addrs = [a.strip() for a in to_addr.split(",")]
    try:
        ctx_ssl = ssl.create_default_context()
        with smtplib.SMTP_SSL(
            settings.email_smtp_host, settings.email_smtp_port, context=ctx_ssl, timeout=30
        ) as s:
            s.login(smtp_user, settings.email_smtp_password)
            s.sendmail(msg["From"], to_addrs, msg.as_string())
        return True
    except Exception:
        return False


# --- Routes ---


@app.get("/", response_class=HTMLResponse)
async def index():
    today = date.today()
    return _render(
        "index.html",
        cabins=CABIN_CHOICES,
        default_start=today.isoformat(),
        default_end=today.replace(day=28).isoformat() if today.month != 2 else today.replace(day=28).isoformat(),
    )


@app.post("/search", response_class=HTMLResponse)
async def search(
    request: Request,
    origin: Annotated[str, Form()] = "SFO",
    destination: Annotated[str, Form()] = "BJS",
    start_date: Annotated[str, Form()] = "",
    end_date: Annotated[str, Form()] = "",
    cabin: Annotated[str, Form()] = "business",
    max_miles: Annotated[int, Form()] = 110000,
    exclude_airports: Annotated[str, Form()] = "MNL",
):
    params = {
        "origin": origin,
        "destination": destination,
        "start_date": date.fromisoformat(start_date) if start_date else date.today(),
        "end_date": date.fromisoformat(end_date) if end_date else date.today(),
        "cabin": cabin,
        "max_miles": max_miles,
        "exclude_airports": exclude_airports,
    }

    results = _run_scraper(params)

    if isinstance(results, dict) and "error" in results:
        return _render(
            "results.html",
            error=results["error"],
            params=params,
            results=[],
            cabins=CABIN_CHOICES,
        )

    return _render(
        "results.html",
        params=params,
        results=results,
        cabins=CABIN_CHOICES,
    )


@app.post("/email", response_class=HTMLResponse)
async def email_results(
    request: Request,
    email_to: Annotated[str, Form()] = "",
    results_json: Annotated[str, Form()] = "[]",
    origin: Annotated[str, Form()] = "",
    destination: Annotated[str, Form()] = "",
    start_date: Annotated[str, Form()] = "",
    end_date: Annotated[str, Form()] = "",
    cabin: Annotated[str, Form()] = "business",
    max_miles: Annotated[int, Form()] = 110000,
):
    import json

    results = json.loads(results_json)
    params = {
        "origin": origin,
        "destination": destination,
        "start_date": start_date,
        "end_date": end_date,
        "cabin": cabin,
        "max_miles": max_miles,
    }

    to = email_to or settings.email_to
    ok = _send_results_email(to, results, params)

    return _render(
        "results.html",
        params=params,
        results=results,
        cabins=CABIN_CHOICES,
        email_sent=ok,
        email_error="" if ok else "SMTP failed. Check credentials.",
    )


def run(host: str = "127.0.0.1", port: int = 8080):
    import uvicorn

    uvicorn.run(app, host=host, port=port)
