from __future__ import annotations

from html import unescape
from dataclasses import replace
import re
from urllib.parse import parse_qs, urljoin, urlsplit

from bs4 import BeautifulSoup

import requests

from .core import Listing, clean_space, find_price


BASE_URL = "https://reality.bazos.sk"
NEXT_LABEL = "ďalšia"
ITEM_SPLIT = re.compile(r'<div\s+class=["\']inzeraty inzeratyflex["\']>', re.I)
TAG_RE = re.compile(r"<[^>]+>")


class BazosParseError(RuntimeError):
    """Unexpected pagination or response; do not silently truncate the scan."""


def _text(fragment: str) -> str:
    fragment = re.sub(r"<br\s*/?>", " ", fragment, flags=re.I)
    return clean_space(unescape(TAG_RE.sub(" ", fragment)))


def _first(pattern: str, fragment: str) -> str | None:
    match = re.search(pattern, fragment, flags=re.I | re.S)
    return match.group(1) if match else None


def parse_bazos_html(page_html: str) -> list[Listing]:
    listings: list[Listing] = []
    for fragment in ITEM_SPLIT.split(page_html)[1:]:
        href = _first(r'<h2\s+class=["\']?nadpis["\']?>\s*<a\s+href=["\']([^"\']+)', fragment)
        title_html = _first(r'<h2\s+class=["\']?nadpis["\']?>\s*<a[^>]*>(.*?)</a>\s*</h2>', fragment)
        if not href or not title_html:
            continue

        source_id = _first(r"/inzerat/(\d+)/", href)
        if not source_id:
            continue

        description_html = _first(r'<div\s+class=["\']?popis["\']?>\s*(.*?)</div>', fragment) or ""
        price_html = _first(r'<div\s+class=["\']inzeratycena["\']>\s*<b>\s*<span[^>]*>(.*?)</span>', fragment) or ""
        location_html = _first(r'<div\s+class=["\']inzeratylok["\']>(.*?)</div>', fragment) or ""
        image_src = _first(r'<img\s+[^>]*src=["\']([^"\']+)["\'][^>]*class=["\']obrazek["\']', fragment)
        published_at = _first(r"\[(\d{1,2}\.\s*\d{1,2}\.\s*\d{4})\]", fragment)
        if published_at:
            published_at = re.sub(r"\s+", "", published_at)
        price_digits = re.sub(r"\D", "", _text(price_html))

        listings.append(
            Listing(
                source="bazos",
                source_id=source_id,
                url=urljoin(BASE_URL, href),
                title=_text(title_html),
                description=_text(description_html),
                price_eur=int(price_digits) if price_digits else None,
                location=_text(location_html),
                published_at=published_at,
                image_url=urljoin(BASE_URL, image_src) if image_src else None,
            )
        )
    return listings


def parse_bazos_detail(page_html: str, listing: Listing) -> Listing:
    description_html = _first(r'<div\s+class=["\']?popisdetail["\']?>\s*(.*?)</div>', page_html)
    if not description_html:
        return listing
    description = _text(description_html)
    # The detailed monthly price can differ from the search card (e.g. 430 vs 450).
    detail_price = find_price(description)
    return replace(listing, description=description, price_eur=detail_price or listing.price_eur)


class BazosClient:
    def __init__(self, timeout_seconds: int = 20, max_pages: int = 15) -> None:
        self.timeout_seconds = timeout_seconds
        self.max_pages = max(1, max_pages)
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "Chrome/126.0 Safari/537.36 FlatWatch/0.1"
                ),
                "Accept-Language": "sk-SK,sk;q=0.9,en;q=0.7",
            }
        )

    def fetch(self, url: str) -> list[Listing]:
        pending = url
        visited: set[str] = set()
        offers: dict[str, Listing] = {}
        initial = urlsplit(url)
        category = re.match(r"(/prenajmu/[^/]+/)(?:\d+/)?$", initial.path)
        if not category:
            raise BazosParseError('Unexpected Bazoš search URL')
        while pending:
            if pending in visited:
                raise BazosParseError('Bazoš pagination loop')
            if len(visited) >= self.max_pages:
                raise BazosParseError('Bazoš page limit reached; scan incomplete')
            response = self.session.get(pending, timeout=self.timeout_seconds)
            response.raise_for_status()
            soup = BeautifulSoup(response.text, 'html.parser')
            items = parse_bazos_html(response.text)
            if not items and 'Hľadaniu nevyhovujú žiadne inzeráty' not in soup.get_text(' ', strip=True):
                raise BazosParseError('Bazoš listings are unexpectedly empty')
            for item in items:
                offers.setdefault(item.source_id, item)
            visited.add(pending)
            following = None
            for anchor in soup.select('a[href]'):
                if anchor.get_text(' ', strip=True).casefold() != NEXT_LABEL:
                    continue
                candidate = urljoin(response.url, str(anchor['href']))
                parts = urlsplit(candidate)
                if parts.hostname != initial.hostname or not parts.path.startswith(category[1]):
                    raise BazosParseError('Unexpected Bazoš next page URL')
                # Search criteria must remain the same (Bazoš normalizes postal codes).
                original = parse_qs(initial.query)
                newer = parse_qs(parts.query)
                if any(original.get(key) != newer.get(key) for key in ('hledat', 'humkreis', 'cenaod', 'cenado')
                       if original.get(key)):
                    raise BazosParseError('Bazoš pagination changed search criteria')
                following = candidate
                break
            pending = following
        return list(offers.values())

    def enrich(self, listing: Listing) -> Listing:
        response = self.session.get(listing.url, timeout=self.timeout_seconds)
        response.raise_for_status()
        return parse_bazos_detail(response.text, listing)
