from __future__ import annotations

from dataclasses import asdict
from difflib import SequenceMatcher
import json
from pathlib import Path
import re
import sqlite3

from .core import Listing, find_rooms, fold, listing_fingerprint, utc_now


SCHEMA = """
CREATE TABLE IF NOT EXISTS listings (
    source TEXT NOT NULL,
    source_id TEXT NOT NULL,
    url TEXT NOT NULL,
    title TEXT NOT NULL,
    description TEXT NOT NULL,
    price_eur INTEGER,
    location TEXT NOT NULL,
    published_at TEXT,
    image_url TEXT,
    preferred_area TEXT,
    content_fingerprint TEXT,
    metadata TEXT,
    first_seen_at TEXT NOT NULL,
    notified_at TEXT,
    PRIMARY KEY (source, source_id)
);
CREATE INDEX IF NOT EXISTS listings_pending_idx ON listings(notified_at);
CREATE TABLE IF NOT EXISTS source_state (
    source TEXT PRIMARY KEY,
    initialized_at TEXT NOT NULL
);
INSERT OR IGNORE INTO source_state(source, initialized_at)
SELECT source, MIN(first_seen_at) FROM listings GROUP BY source;
"""


def _body_words(text: str) -> str:
    return ' '.join(re.findall(r'\w+', fold(text)))


def _area(text: str) -> str | None:
    match = re.search(r'\b(\d{2,3})\s*m(?:2|²)\b', fold(text))
    return match[1] if match else None


def _street(text: str) -> str | None:
    value = fold(text)
    match = re.search(r'\b(?:ulic[aeiy]|ul\.|street)\s*[:\-]?\s*(\w+)', value)
    if not match:
        match = re.search(r'\bna\s+(\w+)\s+ulic\w*', value)
    return match[1][:5] if match else None


def _same_description(first: Listing, second: Listing) -> bool:
    if first.price_eur is None or first.price_eur != second.price_eur:
        return False
    rooms_a, rooms_b = find_rooms(first.title), find_rooms(second.title)
    if rooms_a and rooms_b and rooms_a != rooms_b:
        return False
    a, b = _body_words(first.description), _body_words(second.description)
    if len(a) < 120 or len(b) < 120:
        return False
    if a == b:
        return True
    title_a, title_b = _body_words(first.title), _body_words(second.title)
    shorter_title, longer_title = sorted((title_a, title_b), key=len)
    shorter_body, longer_body = sorted((a, b), key=len)
    # Older Bazoš rows contain a ~300-character search preview. Only merge a
    # preview with a full ad when a distinctive headline and the body agree.
    if (len(shorter_title) >= 40 and longer_title.startswith(shorter_title)
            and len(shorter_body) >= 160 and
            SequenceMatcher(None, shorter_body, longer_body[:len(shorter_body)],
                            autojunk=False).ratio() >= 0.97):
        return True
    area_a, area_b = _area(a), _area(b)
    street_a = _street(first.title + ' ' + a)
    street_b = _street(second.title + ' ' + b)
    # Merge edited text only when independently identifying facts agree.
    return bool(area_a and area_a == area_b and street_a and street_a == street_b
                and SequenceMatcher(None, a, b).ratio() >= 0.96)


class ListingStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path)
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript(SCHEMA)
        columns = {row["name"] for row in self.connection.execute("PRAGMA table_info(listings)")}
        if "metadata" not in columns:
            self.connection.execute("ALTER TABLE listings ADD COLUMN metadata TEXT")
        self._migrate_content_fingerprints()

    def _migrate_content_fingerprints(self) -> None:
        columns = {
            row["name"] for row in self.connection.execute("PRAGMA table_info(listings)")
        }
        if "content_fingerprint" not in columns:
            self.connection.execute("ALTER TABLE listings ADD COLUMN content_fingerprint TEXT")
        rows = self.connection.execute("SELECT * FROM listings").fetchall()
        for row in rows:
            listing = self._from_row(row)
            self.connection.execute(
                "UPDATE listings SET content_fingerprint = ? WHERE source = ? AND source_id = ?",
                (listing_fingerprint(listing), listing.source, listing.source_id),
            )
        self.connection.execute(
            "CREATE INDEX IF NOT EXISTS listings_fingerprint_idx ON listings(content_fingerprint)"
        )
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()

    def count(self) -> int:
        row = self.connection.execute("SELECT COUNT(*) AS count FROM listings").fetchone()
        return int(row["count"])

    def is_pending(self, listing: Listing) -> bool:
        row = self.connection.execute(
            "SELECT notified_at FROM listings WHERE source = ? AND source_id = ?",
            (listing.source, listing.source_id),
        ).fetchone()
        return bool(row is None or row["notified_at"] is None)

    def exists(self, listing: Listing) -> bool:
        row = self.connection.execute(
            "SELECT 1 FROM listings WHERE source = ? AND source_id = ?",
            (listing.source, listing.source_id),
        ).fetchone()
        return row is not None

    def is_content_duplicate(self, listing: Listing) -> bool:
        row = self.connection.execute(
            """
            SELECT source, source_id
            FROM listings
            WHERE content_fingerprint = ?
              AND NOT (source = ? AND source_id = ?)
              AND (notified_at IS NULL OR notified_at NOT LIKE 'FILTERED:%')
            ORDER BY first_seen_at, source, source_id
            LIMIT 1
            """,
            (listing_fingerprint(listing), listing.source, listing.source_id),
        ).fetchone()
        if row is not None:
            return True
        if listing.price_eur is None or len(listing.description) < 120:
            return False
        candidates = self.connection.execute(
            """SELECT * FROM listings WHERE price_eur = ?
               AND NOT (source = ? AND source_id = ?)
               AND (notified_at IS NULL OR notified_at NOT LIKE 'FILTERED:%')""",
            (listing.price_eur, listing.source, listing.source_id),
        )
        return any(_same_description(listing, self._from_row(other)) for other in candidates)

    def is_source_initialized(self, source: str) -> bool:
        row = self.connection.execute(
            "SELECT 1 FROM source_state WHERE source = ?",
            (source,),
        ).fetchone()
        return row is not None

    def mark_source_initialized(self, source: str) -> None:
        self.connection.execute(
            "INSERT OR IGNORE INTO source_state(source, initialized_at) VALUES (?, ?)",
            (source, utc_now()),
        )
        self.connection.commit()

    def add_if_unique(self, listing: Listing) -> tuple[bool, bool]:
        """Reserve an ad and check duplicates under one SQLite write lock."""
        with self.connection:
            self.connection.execute('BEGIN IMMEDIATE')
            existed = self.exists(listing)
            duplicate = self.is_content_duplicate(listing)
            inserted = self.add(listing, commit=False)
            if duplicate:
                self.mark_filtered(listing.source, listing.source_id, 'duplicate', commit=False)
            return inserted and not duplicate and not existed, duplicate

    def add(self, listing: Listing, *, commit: bool = True) -> bool:
        values = asdict(listing)
        values["metadata"] = json.dumps(listing.metadata, ensure_ascii=False) if listing.metadata is not None else None
        values["first_seen_at"] = values["first_seen_at"] or utc_now()
        values["content_fingerprint"] = listing_fingerprint(listing)
        existing = self.connection.execute(
            "SELECT notified_at FROM listings WHERE source = ? AND source_id = ?",
            (listing.source, listing.source_id),
        ).fetchone()
        if existing:
            self.connection.execute(
                """
                UPDATE listings SET
                    url = :url,
                    title = :title,
                    description = :description,
                    price_eur = :price_eur,
                    location = :location,
                    published_at = :published_at,
                    image_url = :image_url,
                    preferred_area = :preferred_area,
                    content_fingerprint = :content_fingerprint,
                    metadata = :metadata,
                    notified_at = CASE
                        WHEN notified_at LIKE 'FILTERED:%' THEN NULL
                        ELSE notified_at
                    END
                WHERE source = :source AND source_id = :source_id
                """,
                values,
            )
            if commit:
                self.connection.commit()
            return False
        self.connection.execute(
            """
            INSERT INTO listings (
                source, source_id, url, title, description, price_eur, location,
                published_at, image_url, preferred_area, content_fingerprint, first_seen_at, metadata
            ) VALUES (
                :source, :source_id, :url, :title, :description, :price_eur, :location,
                :published_at, :image_url, :preferred_area, :content_fingerprint, :first_seen_at, :metadata
            )
            """,
            values,
        )
        if commit:
            self.connection.commit()
        return True

    def pending(self, sources: set[str] | None = None) -> list[Listing]:
        if sources:
            placeholders = ",".join("?" for _ in sources)
            rows = self.connection.execute(
                f"""SELECT * FROM listings
                WHERE notified_at IS NULL AND source IN ({placeholders})
                ORDER BY first_seen_at, source_id""",
                tuple(sorted(sources)),
            ).fetchall()
        else:
            rows = self.connection.execute(
                "SELECT * FROM listings WHERE notified_at IS NULL ORDER BY first_seen_at, source_id"
            ).fetchall()
        return [self._from_row(row) for row in rows]

    def mark_notified(self, listing: Listing, marker: str | None = None) -> None:
        self.connection.execute(
            "UPDATE listings SET notified_at = ? WHERE source = ? AND source_id = ?",
            (marker or utc_now(), listing.source, listing.source_id),
        )
        self.connection.commit()

    def mark_filtered(self, source: str, source_id: str, reason: str, *, commit: bool = True) -> None:
        self.connection.execute(
            """
            UPDATE listings
            SET notified_at = ?
            WHERE source = ? AND source_id = ? AND notified_at IS NULL
            """,
            (f"FILTERED:{reason}", source, source_id),
        )
        if commit:
            self.connection.commit()

    @staticmethod
    def _from_row(row: sqlite3.Row) -> Listing:
        return Listing(
            source=row["source"],
            source_id=row["source_id"],
            url=row["url"],
            title=row["title"],
            description=row["description"],
            price_eur=row["price_eur"],
            location=row["location"],
            published_at=row["published_at"],
            image_url=row["image_url"],
            preferred_area=row["preferred_area"],
            first_seen_at=row["first_seen_at"],
            metadata=json.loads(row["metadata"]) if row["metadata"] else None,
        )
