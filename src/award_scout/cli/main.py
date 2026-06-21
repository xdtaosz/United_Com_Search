"""CLI entry point for award_scout."""

from __future__ import annotations

import asyncio
from datetime import date, datetime
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table
from rich.progress import Progress, SpinnerColumn, TextColumn

from award_scout.config import settings
from award_scout.db.repository import Database
from award_scout.models import AwardOffer, CabinClass, SearchQuery

app = typer.Typer(
    name="award-scout",
    help="US airline award miles flight search tool",
    no_args_is_help=True,
)
console = Console()


def run_async(coro):
    return asyncio.run(coro)


# --- Init ---

@app.command()
def init():
    """Initialize the database and check configuration."""
    db = Database.get_instance()
    db.init_db()
    console.print("[green]✓[/green] Database initialized at " + str(settings.db_path))

    missing = []
    if not settings.united_mp_number:
        missing.append("UNITED_MP_NUMBER")
    if not settings.united_password:
        missing.append("UNITED_PASSWORD")

    if missing:
        console.print(
            "[yellow]![/yellow] Missing config values: "
            + ", ".join(missing)
            + "\n  Set them in .env or export as environment variables."
        )
    else:
        console.print("[green]✓[/green] United credentials found")


# --- Search ---

@app.command()
def search(
    origin: str = typer.Argument(..., help="Origin airport IATA code"),
    destination: str = typer.Argument(..., help="Destination airport IATA code"),
    date_str: str = typer.Option(..., "--date", "-d", help="Departure date (YYYY-MM-DD)"),
    return_date_str: Optional[str] = typer.Option(
        None, "--return", "-r", help="Return date (YYYY-MM-DD)"
    ),
    cabin: str = typer.Option("economy", "--cabin", "-c", help="Cabin: economy, premium_economy, business, first"),
    max_miles: Optional[int] = typer.Option(None, "--max-miles", "-m", help="Maximum miles required"),
    max_stops: Optional[int] = typer.Option(None, "--max-stops", "-s", help="Maximum stops"),
    airline: Optional[str] = typer.Option(
        None, "--airline", "-a", help="Airline to search (united, american)"
    ),
    limit: int = typer.Option(50, "--limit", "-l", help="Max results to show"),
):
    """Search award flight availability."""
    depart_date = date.fromisoformat(date_str)
    return_date = date.fromisoformat(return_date_str) if return_date_str else None
    cabin_class = CabinClass(cabin)

    query = SearchQuery(
        origin=origin,
        destination=destination,
        depart_date=depart_date,
        return_date=return_date,
        cabin=cabin_class,
        airlines=[airline] if airline else None,
    )

    db = Database.get_instance()
    db.init_db()

    search_id = db.save_search(query)

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
    ) as progress:
        all_offers: list[AwardOffer] = []

        if airline is None or airline == "united":
            task = progress.add_task("Searching United...", total=None)
            from award_scout.scrapers.united import UnitedScraper

            async def search_united():
                async with UnitedScraper() as scraper:
                    return await scraper.search(query)

            try:
                united_offers = run_async(search_united())
                all_offers.extend(united_offers)
                progress.update(task, description=f"United: {len(united_offers)} offers found")
            except Exception as e:
                progress.update(task, description=f"[red]United failed: {e}")
            finally:
                progress.remove_task(task)

        if airline is None or airline == "american":
            task = progress.add_task("Searching American Airlines...", total=None)
            from award_scout.scrapers.american import AmericanScraper

            async def search_american():
                async with AmericanScraper() as scraper:
                    return await scraper.search(query)

            try:
                aa_offers = run_async(search_american())
                all_offers.extend(aa_offers)
                progress.update(task, description=f"American: {len(aa_offers)} offers found")
            except Exception as e:
                progress.update(task, description=f"[red]American failed: {e}")
            finally:
                progress.remove_task(task)

    # Save to DB
    if all_offers:
        db.save_offers(search_id, all_offers)

    # Filter results
    filtered = _filter_offers(all_offers, max_miles, max_stops)

    # Display results
    if not filtered:
        console.print("[yellow]No award offers found matching your criteria.[/yellow]")
        return

    _display_offers(filtered)
    console.print(f"\n[dim]Showing {len(filtered)} of {len(all_offers)} total offers[/dim]")


# --- Query (from database) ---

@app.command()
def query(
    origin: str = typer.Argument(..., help="Origin airport IATA code"),
    destination: str = typer.Argument(..., help="Destination airport IATA code"),
    cabin: Optional[str] = typer.Option(None, "--cabin", "-c", help="Filter by cabin"),
    max_miles: Optional[int] = typer.Option(None, "--max-miles", "-m", help="Maximum miles"),
    max_stops: Optional[int] = typer.Option(None, "--max-stops", "-s", help="Maximum stops"),
    airline: Optional[str] = typer.Option(None, "--airline", "-a", help="Filter by airline"),
    limit: int = typer.Option(50, "--limit", "-l", help="Max results"),
):
    """Query cached award results from the database."""
    db = Database.get_instance()
    db.init_db()

    offers = db.query_offers(
        origin=origin,
        destination=destination,
        cabin=cabin,
        max_miles=max_miles,
        max_stops=max_stops,
        airlines=[airline] if airline else None,
        limit=limit,
    )

    if not offers:
        console.print("[yellow]No cached results found. Run 'search' first.[/yellow]")
        return

    _display_offers(offers)
    console.print(f"\n[dim]Showing {len(offers)} results from database[/dim]")


# --- Watch commands ---

@app.command()
def watch(
    origin: Optional[str] = typer.Argument(None, help="Origin airport IATA code"),
    destination: Optional[str] = typer.Argument(None, help="Destination airport IATA code"),
    start_date: Optional[str] = typer.Option(None, "--start", help="Start date (YYYY-MM-DD)"),
    end_date: Optional[str] = typer.Option(None, "--end", help="End date (YYYY-MM-DD)"),
    cabin: Optional[str] = typer.Option(None, "--cabin", "-c", help="Cabin filter"),
    max_miles: Optional[int] = typer.Option(None, "--max-miles", "-m", help="Max miles"),
    max_stops: Optional[int] = typer.Option(None, "--max-stops", "-s", help="Max stops"),
    notify_via: Optional[str] = typer.Option(None, "--notify", "-n", help="Notification method (ntfy, email)"),
    notify_target: Optional[str] = typer.Option(None, "--notify-target", help="Notification target (ntfy topic or email address)"),
):
    """Add a watch rule for continuous monitoring. Uses .env defaults if arguments omitted."""
    from award_scout.models import WatchRule as WatchRuleModel

    rule = WatchRuleModel(
        origin=origin or settings.watch_origin or "",
        destination=destination or settings.watch_destination or "",
        start_date=date.fromisoformat(start_date) if start_date else (date.fromisoformat(settings.watch_start_date) if settings.watch_start_date else None),
        end_date=date.fromisoformat(end_date) if end_date else (date.fromisoformat(settings.watch_end_date) if settings.watch_end_date else None),
        cabin=CabinClass(cabin) if cabin else (CabinClass(settings.watch_cabin) if settings.watch_cabin else None),
        max_miles=max_miles or settings.watch_max_miles,
        max_stops=max_stops,
        notify_via=notify_via,
        notify_target=notify_target,
    )

    db = Database.get_instance()
    db.init_db()
    db.save_watch(rule)
    console.print(f"[green]✓[/green] Watch rule added: {rule.origin} → {rule.destination}")


@app.command()
def watches():
    """List all active watch rules."""
    db = Database.get_instance()
    db.init_db()
    rules = db.get_all_watches()

    if not rules:
        console.print("[yellow]No watch rules defined.[/yellow]")
        return

    table = Table(title="Watch Rules")
    table.add_column("ID", style="dim")
    table.add_column("Route")
    table.add_column("Dates")
    table.add_column("Cabin")
    table.add_column("Max Miles")
    table.add_column("Active")
    table.add_column("Last Checked")

    for r in rules:
        dates = f"{r.start_date or '—'} → {r.end_date or '—'}"
        table.add_row(
            r.id[:8],
            f"{r.origin} → {r.destination}",
            dates,
            r.cabin.value if r.cabin else "any",
            str(r.max_miles or "—"),
            "[green]✓[/green]" if r.active else "[red]✗[/red]",
            r.last_checked or "never",
        )

    console.print(table)


@app.command()
def unwatch(watch_id: str = typer.Argument(..., help="Watch rule ID to remove")):
    """Remove a watch rule."""
    db = Database.get_instance()
    db.init_db()
    db.deactivate_watch(watch_id)
    console.print(f"[green]✓[/green] Watch rule {watch_id[:8]} deactivated")


# --- Login ---

@app.command()
def login(
    airline: str = typer.Argument(..., help="Airline to login: united, american, delta, alaska"),
):
    """Log in to an airline once to persist session. Subsequent searches skip MFA."""
    airline_lower = airline.lower()

    async def _mfa_prompt():
        return typer.prompt(
            "MFA verification code (check email/phone/SMS)",
            hide_input=False,
        )

    if airline_lower == "united":
        from award_scout.scrapers.united import UnitedScraper

        async def _login():
            async with UnitedScraper() as scraper:
                scraper.set_mfa_callback(_mfa_prompt)
                ok = await scraper.login()
                if ok:
                    token = scraper.bearer_token
                    return token
                return None

        token = run_async(_login())
        if token:
            console.print(
                f"[green]✓[/green] United login successful. Session saved to "
                f"{settings.sessions_dir}/united_session.json"
            )
            console.print(f"[dim]  Bearer token: {token[:20]}...[/dim]")
        else:
            console.print("[red]✗[/red] United login failed.")
            raise typer.Exit(1)

    elif airline_lower == "american":
        from award_scout.scrapers.american import AmericanScraper

        async def _login():
            async with AmericanScraper() as scraper:
                scraper.set_mfa_callback(_mfa_prompt)
                ok = await scraper.login()
                return "ok" if ok else None

        result = run_async(_login())
        if result:
            console.print(f"[green]✓[/green] American Airlines login successful.")
        else:
            console.print("[red]✗[/red] American Airlines login failed.")
            raise typer.Exit(1)

    else:
        console.print(f"[red]✗ Unsupported airline: {airline}. Options: united, american[/red]")
        raise typer.Exit(1)


# --- Check watches ---

@app.command()
def check():
    """Run all active watch rules and check for new deals."""
    from award_scout.monitors import run_watches

    run_async(run_watches())


# --- Cron ---

@app.command()
def cron(
    action: str = typer.Argument(
        "install", help="Action: install / uninstall / status"
    ),
):
    """Install or remove the cron job for award-scout check (every 4 hours)."""
    import shutil
    import subprocess
    import sys
    from pathlib import Path

    scout_bin = shutil.which("award-scout")
    if scout_bin is None:
        scout_bin = str(Path(sys.executable).parent / "award-scout")
        if not Path(scout_bin).is_file():
            console.print(
                "[red]✗[/red] award-scout not found on PATH. "
                "Run 'pip install -e .' first."
            )
            raise typer.Exit(1)

    log_dir = settings.data_path
    log_file = log_dir / "cron.log"
    project_dir = Path(__file__).resolve().parent.parent.parent.parent

    cron_line = (
        f"0 */4 * * * cd {project_dir} && {scout_bin} check >> {log_file} 2>&1\n"
    )

    if action == "status":
        result = subprocess.run(
            ["crontab", "-l"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            console.print("[yellow]No crontab installed.[/yellow]")
        elif "award-scout check" in result.stdout:
            console.print("[green]✓[/green] Cron job is installed (every 4 hours)")
            console.print(result.stdout)
        else:
            console.print("[yellow]Crontab exists but no award-scout job found:[/yellow]")
            console.print(result.stdout)

    elif action == "install":
        result = subprocess.run(
            ["crontab", "-l"],
            capture_output=True, text=True, timeout=10,
        )
        existing = result.stdout if result.returncode == 0 else ""
        if "award-scout check" in existing:
            console.print("[green]✓[/green] Cron job already installed")
            return

        new_cron = existing + cron_line
        p = subprocess.run(
            ["crontab", "-"],
            input=new_cron, capture_output=True, text=True, timeout=10,
        )
        if p.returncode == 0:
            console.print(
                f"[green]✓[/green] Cron job installed (every 4 hours)\n"
                f"  {scout_bin} check → {log_file}\n"
                f"  Runs at :00 past every 4th hour"
            )
        else:
            console.print(f"[red]✗[/red] Failed to install cron: {p.stderr}")

    elif action == "uninstall":
        result = subprocess.run(
            ["crontab", "-l"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            console.print("[yellow]No crontab to remove.[/yellow]")
            return

        lines = [l for l in result.stdout.splitlines(keepends=True)
                 if "award-scout check" not in l]
        p = subprocess.run(
            ["crontab", "-"],
            input="".join(lines), capture_output=True, text=True, timeout=10,
        )
        if p.returncode == 0:
            console.print("[green]✓[/green] Cron jobs removed")
        else:
            console.print(f"[red]✗[/red] Failed to remove cron: {p.stderr}")

    else:
        console.print(f"[red]Unknown action: {action}. Use install/uninstall/status.[/red]")


# --- Internal helpers ---

def _filter_offers(
    offers: list[AwardOffer],
    max_miles: Optional[int] = None,
    max_stops: Optional[int] = None,
) -> list[AwardOffer]:
    filtered = offers
    if max_miles is not None:
        filtered = [o for o in filtered if o.miles_required <= max_miles]
    if max_stops is not None:
        filtered = [o for o in filtered if o.stops <= max_stops]
    return sorted(filtered, key=lambda o: (o.miles_required, o.taxes_fees))


def _display_offers(offers: list[AwardOffer]) -> None:
    if not offers:
        return

    table = Table(title=f"Award Flights — {offers[0].query_origin} → {offers[0].query_destination}")
    table.add_column("Airline")
    table.add_column("Date")
    table.add_column("Stops")
    table.add_column("Duration")
    table.add_column("Cabin")
    table.add_column("Miles")
    table.add_column("Taxes")
    table.add_column("Seats")
    table.add_column("Flight")

    for o in offers:
        first_seg = o.segments[0] if o.segments else None
        last_seg = o.segments[-1] if o.segments else None
        flight_info = ""
        if first_seg and last_seg:
            dep_time = _fmt_time(first_seg.departure_time)
            arr_time = _fmt_time(last_seg.arrival_time)
            flight_info = f"{first_seg.flight_number} {dep_time}–{arr_time}"

        duration_hrs = f"{o.total_duration_minutes // 60}h{o.total_duration_minutes % 60}m" if o.total_duration_minutes else "—"

        table.add_row(
            o.source_airline.upper(),
            o.depart_date,
            str(o.stops) if o.stops > 0 else "[green]nonstop[/green]",
            duration_hrs,
            o.cabin.value.title(),
            f"[bold]{o.miles_required:,}[/bold]",
            f"${o.taxes_fees:.2f}",
            str(o.total_seats_available),
            flight_info,
        )

    console.print(table)


def _fmt_time(iso_or_raw: str) -> str:
    """Extract a short time string from various time formats."""
    if not iso_or_raw:
        return ""
    # If it's ISO format
    try:
        dt = datetime.fromisoformat(iso_or_raw.replace("Z", "+00:00"))
        return dt.strftime("%H:%M")
    except ValueError:
        pass
    # Try common formats
    for fmt in ["%Y-%m-%dT%H:%M", "%H:%M", "%I:%M %p"]:
        try:
            dt = datetime.strptime(iso_or_raw[:16], fmt)
            return dt.strftime("%H:%M")
        except ValueError:
            continue
    return iso_or_raw[:5] if len(iso_or_raw) >= 5 else iso_or_raw


if __name__ == "__main__":
    app()
