from __future__ import annotations

import argparse
import json
from pathlib import Path

from flatwatch.app import ROOT, load_config, load_dotenv, notifier_from_config
from flatwatch.history import collect_history, write_history_report
from flatwatch.storage import ListingStore
from flatwatch.core import Listing, matches_filters, listing_fingerprint


def was_delivered(marker: str | None) -> bool:
    if not marker or marker == "BASELINE" or marker.startswith("FILTERED:"):
        return False
    return True


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Send matching Facebook listings that were missed during an outage"
    )
    parser.add_argument("--config", default="config.json")
    parser.add_argument("--hours", type=float, default=24)
    parser.add_argument("--scrolls", type=int, default=8)
    parser.add_argument("--output", default="data/facebook-last-24h.md")
    parser.add_argument('--input', help='Previously reviewed Listing JSON; no browsing')
    parser.add_argument('--send', action='store_true', help='Explicitly send accepted items')
    args = parser.parse_args()

    load_dotenv(ROOT / ".env")
    config = load_config(args.config)
    hours = max(1, args.hours)
    if args.input:
        raw = [Listing(**row) for row in json.loads(Path(args.input).read_text())]
        listings, rejected, fetched = [], {}, len(raw)
        for item in raw:
            accepted, reason, enriched = matches_filters(item, {**config['filters'], 'max_age_hours': hours})
            if accepted:
                listings.append(enriched)
            else:
                rejected[reason] = rejected.get(reason, 0) + 1
    else:
        listings, rejected, fetched = collect_history(
            config, hours=hours, include_bazos=False, include_facebook=True,
            include_jozue=False,
            facebook_scrolls=max(1, args.scrolls),
        )

    output_path = Path(args.output)
    if not output_path.is_absolute():
        output_path = ROOT / output_path
    write_history_report(
        listings,
        output_path,
        hours=hours,
        fetched=fetched,
        rejected=rejected,
    )

    if not args.send:
        print(json.dumps({'dry_run': True, 'fetched': fetched, 'matched': len(listings), 'rejected': rejected}, ensure_ascii=False))
        return 0
    store = ListingStore(config["database"])
    notifier = notifier_from_config(config, required=True)
    sent = []
    skipped_delivered = 0
    skipped_duplicates = 0
    failed = []
    try:
        for listing in listings:
            row = store.connection.execute(
                "SELECT notified_at FROM listings WHERE source = ? AND source_id = ?",
                (listing.source, listing.source_id),
            ).fetchone()
            marker = row["notified_at"] if row else None
            if was_delivered(marker):
                skipped_delivered += 1
                continue
            # BASELINE is not delivery: allow one representative of an unsent crosspost.
            related = store.connection.execute('SELECT notified_at FROM listings WHERE content_fingerprint=?', (listing_fingerprint(listing),)).fetchall()
            if any(was_delivered(r['notified_at']) for r in related):
                store.add(listing)
                store.mark_filtered(listing.source, listing.source_id, "duplicate")
                skipped_duplicates += 1
                continue

            store.add(listing)
            try:
                notifier.send_listing(listing)
            except Exception as exc:
                failed.append({"url": listing.url, "error": type(exc).__name__})
                continue
            store.mark_notified(listing)
            sent.append(
                {
                    "price_eur": listing.price_eur,
                    "published_at": listing.published_at,
                    "title": listing.title,
                    "url": listing.url,
                }
            )
    finally:
        store.close()

    print(
        json.dumps(
            {
                "fetched": fetched,
                "matched": len(listings),
                "sent": sent,
                "skipped_delivered": skipped_delivered,
                "skipped_duplicates": skipped_duplicates,
                "failed": failed,
                "rejected": rejected,
                "report": str(output_path),
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
