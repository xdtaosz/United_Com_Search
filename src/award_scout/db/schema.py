"""Database schema and initialization."""

CREATE_SEARCHES_TABLE = """
CREATE TABLE IF NOT EXISTS searches (
    id TEXT PRIMARY KEY,
    origin TEXT NOT NULL,
    destination TEXT NOT NULL,
    depart_date TEXT NOT NULL,
    return_date TEXT,
    cabin TEXT NOT NULL,
    passengers INTEGER DEFAULT 1,
    airlines TEXT,
    created_at TEXT NOT NULL,
    status TEXT DEFAULT 'pending'
);
"""

CREATE_OFFERS_TABLE = """
CREATE TABLE IF NOT EXISTS award_offers (
    id TEXT PRIMARY KEY,
    search_id TEXT NOT NULL,
    source_airline TEXT NOT NULL,
    query_origin TEXT NOT NULL,
    query_destination TEXT NOT NULL,
    depart_date TEXT NOT NULL,
    return_date TEXT,
    stops INTEGER DEFAULT 0,
    total_duration_minutes INTEGER DEFAULT 0,
    miles_required INTEGER NOT NULL,
    taxes_fees REAL DEFAULT 0.0,
    cabin TEXT NOT NULL,
    total_seats_available INTEGER DEFAULT 0,
    segments_json TEXT,
    booking_link TEXT,
    scraped_at TEXT NOT NULL,
    FOREIGN KEY (search_id) REFERENCES searches(id)
);
"""

CREATE_WATCHES_TABLE = """
CREATE TABLE IF NOT EXISTS watch_rules (
    id TEXT PRIMARY KEY,
    origin TEXT NOT NULL,
    destination TEXT NOT NULL,
    start_date TEXT,
    end_date TEXT,
    cabin TEXT,
    max_miles INTEGER,
    max_stops INTEGER,
    airlines TEXT,
    active INTEGER DEFAULT 1,
    notify_via TEXT,
    notify_target TEXT,
    created_at TEXT NOT NULL,
    last_checked TEXT
);
"""

CREATE_OFFERS_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_offers_route ON award_offers(query_origin, query_destination);
CREATE INDEX IF NOT EXISTS idx_offers_date ON award_offers(depart_date);
CREATE INDEX IF NOT EXISTS idx_offers_miles ON award_offers(miles_required);
CREATE INDEX IF NOT EXISTS idx_offers_cabin ON award_offers(cabin);
CREATE INDEX IF NOT EXISTS idx_offers_airline ON award_offers(source_airline);
CREATE INDEX IF NOT EXISTS idx_offers_search ON award_offers(search_id);
"""

ALL_SCHEMA_SQL = [
    CREATE_SEARCHES_TABLE,
    CREATE_OFFERS_TABLE,
    CREATE_WATCHES_TABLE,
    CREATE_OFFERS_INDEXES,
]


def get_schema_version() -> int:
    return 1
