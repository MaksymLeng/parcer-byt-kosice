"""Public Košice rental catalogue on Nehnutelnosti.sk."""
from __future__ import annotations

from datetime import datetime
import json
import re
import time
from urllib.parse import parse_qs, urljoin, urlsplit

from bs4 import BeautifulSoup
import requests

from .core import Listing, clean_space

RENTALS_URL = 'https://www.nehnutelnosti.sk/vysledky/byty/kosice/prenajom'
DETAIL_PATH = re.compile(r'/detail/([^/]+)/[^/]+/?$')


class NehnutelnostiParseError(RuntimeError):
    """The catalogue changed or pagination was incomplete."""


def _page_number(url: str) -> int:
    parsed = urlsplit(url)
    if parsed.hostname != 'www.nehnutelnosti.sk' or parsed.path.rstrip('/') != '/vysledky/byty/kosice/prenajom':
        raise NehnutelnostiParseError(f'Unexpected Nehnutelnosti page: {url}')
    values = parse_qs(parsed.query).get('page', ['1'])
    if len(values) != 1 or not values[0].isdigit() or int(values[0]) < 1:
        raise NehnutelnostiParseError('Invalid Nehnutelnosti page number')
    return int(values[0])


def parse_nehnutelnosti_html(html: str, page_url: str = RENTALS_URL) -> tuple[list[Listing], str | None]:
    _page_number(page_url)
    soup = BeautifulSoup(html, 'html.parser')
    entries = None
    for node in soup.select('script[type="application/ld+json"]'):
        try:
            data = json.loads(node.string or node.get_text())
        except (ValueError, TypeError):
            continue
        graph = data.get('@graph', []) if isinstance(data, dict) else []
        page = next((item for item in graph if item.get('@type') == 'SearchResultsPage'), None)
        if page:
            entries = page.get('mainEntity', {}).get('itemListElement')
            break
    if not isinstance(entries, list):
        raise NehnutelnostiParseError('Nehnutelnosti results JSON-LD is missing')
    if not entries:
        raise NehnutelnostiParseError('Nehnutelnosti results are unexpectedly empty')
    listings: list[Listing] = []
    for entry in entries:
        item = entry.get('item', {})
        url = item.get('url', '')
        parts = urlsplit(url)
        match = DETAIL_PATH.fullmatch(parts.path)
        if parts.hostname != 'www.nehnutelnosti.sk' or not match:
            raise NehnutelnostiParseError('Invalid Nehnutelnosti listing URL')
        title = item.get('name', '')
        description = item.get('description', '')
        published = item.get('validFrom')
        if not title or not description or not isinstance(published, str):
            raise NehnutelnostiParseError('Nehnutelnosti title, body or original publication date is missing')
        try:
            if datetime.fromisoformat(published.replace('Z', '+00:00')).tzinfo is None:
                raise ValueError('timestamp has no timezone')
        except ValueError as exc:
            raise NehnutelnostiParseError('Invalid Nehnutelnosti publication date') from exc
        price = item.get('priceSpecification', {}).get('price')
        if price is not None:
            try:
                price = int(price)
            except (ValueError, TypeError) as exc:
                raise NehnutelnostiParseError('Invalid Nehnutelnosti price') from exc
        images = item.get('image') or []
        if isinstance(images, str):
            images = [images]
        listings.append(Listing(
            source='nehnutelnosti', source_id=match[1], url=url, title=clean_space(title),
            description=clean_space(description), price_eur=price, location='Košice',
            published_at=published, image_url=images[0] if images else None,
            metadata={'property_type': item.get('category', ''),
                      'availability': item.get('availability', '')},
        ))
    next_page = None
    for anchor in soup.select('a[href]'):
        if clean_space(anchor.get_text(' ', strip=True)).casefold() != 'ďalšie':
            continue
        candidate = urljoin(page_url, anchor['href'])
        if _page_number(candidate) != _page_number(page_url) + 1:
            raise NehnutelnostiParseError('Unexpected Nehnutelnosti next page')
        next_page = candidate
        break
    return listings, next_page


class NehnutelnostiClient:
    def __init__(self, timeout_seconds: int = 25, max_pages: int = 15,
                 request_delay_seconds: float = 1.0) -> None:
        self.timeout_seconds = timeout_seconds
        self.max_pages = max(1, max_pages)
        self.request_delay_seconds = max(0.0, request_delay_seconds)
        self.session = requests.Session()
        self.session.headers.update({'User-Agent': 'Mozilla/5.0 FlatWatch/0.1',
                                     'Accept-Language': 'sk-SK,sk;q=0.9'})

    def fetch(self, url: str = RENTALS_URL) -> list[Listing]:
        try:
            pending = url
            pages: set[str] = set()
            offers: dict[str, Listing] = {}
            while pending:
                if pending in pages:
                    raise NehnutelnostiParseError('Nehnutelnosti pagination loop')
                if len(pages) >= self.max_pages:
                    raise NehnutelnostiParseError('Nehnutelnosti page limit reached; scan incomplete')
                time.sleep(self.request_delay_seconds)
                response = self.session.get(pending, timeout=self.timeout_seconds)
                response.raise_for_status()
                if _page_number(response.url) != _page_number(pending):
                    raise NehnutelnostiParseError('Nehnutelnosti redirected to another page')
                items, following = parse_nehnutelnosti_html(response.text, response.url)
                pages.add(pending)
                for item in items:
                    offers.setdefault(item.source_id, item)
                pending = following
            return list(offers.values())
        finally:
            self.session.close()
