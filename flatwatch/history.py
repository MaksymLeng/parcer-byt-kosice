from __future__ import annotations

from pathlib import Path

from .bazos import BazosClient
from .core import Listing, clean_space, listing_age_hours, matches_filters, utilities_status
from .facebook import fetch_facebook_posts
from .jozue import JozueClient
from .nehnutelnosti import NehnutelnostiClient


def collect_history(
    config: dict,
    *,
    hours: float = 48,
    include_bazos: bool = True,
    include_facebook: bool = True,
    include_jozue: bool = True,
    include_nehnutelnosti: bool = True,
    facebook_scrolls: int = 8,
) -> tuple[list[Listing], dict[str, int], int]:
    raw: list[Listing] = []
    sources = config["sources"]

    bazos = sources.get("bazos", {})
    if include_bazos and bazos.get("enabled", True):
        client = BazosClient(max_pages=int(bazos.get('max_pages', 15)))
        seen: set[str] = set()
        for url in bazos.get("urls") or [bazos["url"]]:
            for listing in client.fetch(url):
                if listing.source_id in seen:
                    continue
                seen.add(listing.source_id)
                try:
                    listing = client.enrich(listing)
                except Exception:
                    pass
                raw.append(listing)

    facebook = sources.get("facebook", {})
    if include_facebook and facebook.get("enabled", False):
        raw.extend(
            fetch_facebook_posts(
                facebook["profile_dir"],
                facebook.get("groups", []),
                bool(facebook.get("headless", False)),
                max_posts_per_group=80,
                scroll_rounds=facebook_scrolls,
                chronological=True,
            )
        )

    jozue = sources.get("jozue", {})
    if include_jozue and jozue.get("enabled", False):
        raw.extend(JozueClient(max_pages=int(jozue.get("max_pages", 10))).fetch(jozue["url"]))
    nehnutelnosti = sources.get('nehnutelnosti', {})
    if include_nehnutelnosti and nehnutelnosti.get('enabled', False):
        raw.extend(NehnutelnostiClient(max_pages=int(nehnutelnosti.get('max_pages', 15))).fetch(nehnutelnosti['url']))

    history_filters = dict(config["filters"])
    history_filters["max_age_hours"] = hours
    matched: list[Listing] = []
    rejected: dict[str, int] = {}
    for listing in raw:
        accepted, reason, enriched = matches_filters(listing, history_filters)
        if accepted:
            matched.append(enriched)
        else:
            rejected[reason] = rejected.get(reason, 0) + 1

    matched.sort(key=lambda item: listing_age_hours(item) or 10**9)
    return matched, rejected, len(raw)


def write_history_report(
    listings: list[Listing],
    path: Path,
    *,
    hours: float,
    fetched: int,
    rejected: dict[str, int],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        f"# Квартиры в Košice за последние {hours:g} часов",
        "",
        f"Проверено публикаций: {fetched}. Подходящих: {len(listings)}.",
        "",
    ]
    if not listings:
        lines.append("Подходящих объявлений за этот период не найдено.")
    for index, listing in enumerate(listings, 1):
        utilities = {
            "included": "коммунальные включены",
            "extra": "коммунальные отдельно",
            "unknown": "коммунальные нужно уточнить",
        }[utilities_status(listing.text)]
        priority = f"; приоритет: {listing.preferred_area}" if listing.preferred_area else ""
        summary = clean_space(listing.description)
        if len(summary) > 500:
            summary = summary[:497].rstrip() + "…"
        lines.extend(
            [
                "",
                f"## {index}. {listing.title}",
                "",
                f"- Цена: {listing.price_eur} €; {utilities}{priority}",
                f"- Опубликовано: {listing.published_at or 'время не указано'}",
                f"- Источник: {listing.source.upper()}",
                f"- Ссылка: {listing.url}",
                "",
                summary,
            ]
        )
    lines.extend(["", "## Статистика фильтра", "", str(dict(sorted(rejected.items()))), ""])
    path.write_text("\n".join(lines), encoding="utf-8")
