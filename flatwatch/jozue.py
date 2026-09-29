"""Public Jozue rental catalogue, including pagination and original publication dates."""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_CEILING
import re
import time
from urllib.parse import urljoin, urlsplit
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup
import requests

from .core import Listing, clean_space, fold


BASE_URL = "https://www.jozue.sk"
RENTALS_URL = BASE_URL + "/prenajom-nehnutelnosti"
MONTHS = {name: index for index, name in enumerate(
    ("januar", "februar", "marec", "april", "maj", "jun", "jul", "august",
     "september", "oktober", "november", "december"), 1
)}


class JozueParseError(RuntimeError):
    """Unexpected source structure: do not silently report a successful empty scan."""


def _text(node) -> str:
    return clean_space(node.get_text(" ", strip=True)) if node else ""


def parse_jozue_date(value: str) -> str | None:
    match = re.fullmatch(r"(\d{1,2})\.\s*([a-z]+)\s+(\d{4})\s+(\d{1,2}):(\d{2})(?:\s*hod\.?)?", fold(value).strip())
    if not match or match[2] not in MONTHS:
        return None
    try:
        published = datetime(int(match[3]), MONTHS[match[2]], int(match[1]),
                             int(match[4]), int(match[5]), tzinfo=ZoneInfo("Europe/Bratislava"))
    except ValueError:
        return None
    return published.astimezone(timezone.utc).isoformat()


def parse_jozue_price(value: str) -> int | None:
    # Only the dedicated price field, never a deposit/area/nearby recommended ad.
    match = re.search(r"(?<!\d)(\d[\d .\u00a0]*(?:,\d{1,2}|,-)?)\s*(?:€|EUR)(?![a-z])", value, re.I)
    if not match:
        return None
    number = re.sub(r"[ .\u00a0]", "", match[1]).replace(",-", "").replace(",", ".")
    try:
        return int(Decimal(number).to_integral_value(rounding=ROUND_CEILING))
    except InvalidOperation:
        return None


def _source_url(href: str, page_url: str) -> str:
    url = urlsplit(urljoin(page_url, href))
    if url.scheme not in {"http", "https"} or url.hostname not in {"www.jozue.sk", "jozue.sk"}:
        raise JozueParseError("Unexpected off-site Jozue link")
    path = url.path.rstrip("/")
    # On this site /1 (without the slash) redirects to /404, not page 2.
    if re.fullmatch(r"/prenajom-nehnutelnosti/\d+", path):
        path += "/"
    return BASE_URL + path


def _source_id(url: str) -> str:
    match = re.search(r"-(\d+)$", urlsplit(url).path.rstrip("/"))
    if not match:
        raise JozueParseError("Jozue offer URL has no numeric ID")
    return match[1]


def is_residential(metadata: dict[str, str]) -> bool:
    return bool(re.fullmatch(r"(?:\d+[- ]izbovy byt|garsonka|izba|izby|spolubyvanie)(?:\s*[-–]\s*novostavba)?",
                             fold(metadata.get("property_type", ""))))


def _monthly_price(description: str, metadata: dict[str, str]) -> int | None:
    """Prefer the larger explicit rent when header and body disagree; never use a deposit."""
    header = parse_jozue_price(metadata["price_text"])
    if header is None:
        return None
    stated_rents = []
    for match in re.finditer(
        r"\b(?:najomne(?:ho)?|najom|cena(?:\s+prenajmu)?)\s*"
        r"(?:(?:s\s+energiami|vratane\s+energii|je|vo\s+vyske|mesacne|:|=)\s*){0,5}"
        r"\d[\d .]*(?:,\d{1,2}|,-)?\s*(?:€|eur)(?![a-z])", fold(description)
    ):
        amount = parse_jozue_price(match[0])
        if amount is not None:
            stated_rents.append((amount, match[0]))
    chosen = max([header] + [amount for amount, _ in stated_rents])
    if any(amount != header for amount, _ in stated_rents):
        values = ", ".join(str(amount) for amount in sorted({amount for amount, _ in stated_rents}))
        metadata["price_note"] = f"В ценовом поле {header} €, в описании аренда {values} €. Уточните цену."
    if any(amount == chosen and re.search(r"s\s+energiami|vratane\s+energii", phrase)
           for amount, phrase in stated_rents):
        metadata["utilities_included"] = "true"
    return chosen


def parse_jozue_html(page_html: str, page_url: str = RENTALS_URL) -> tuple[list[Listing], list[str]]:
    soup = BeautifulSoup(page_html, "html.parser")
    grid = soup.select_one("#ponuky_grid")
    if grid is None:
        raise JozueParseError("Jozue rental grid is missing")
    cards = grid.select(".offer")
    if not cards and not re.search(r"ziadne ponuky|nenasli|neboli najdene", fold(_text(soup.select_one("#offers")))):
        raise JozueParseError("Jozue rental grid is unexpectedly empty")
    listings = []
    for card in cards:
        heading = card.select_one(".text-wrapper h2")
        link = heading.find_parent("a") if heading else None
        if not link or not link.get("href"):
            raise JozueParseError("Jozue card title/link is missing")
        url = _source_url(link["href"], page_url)
        fields = {}
        for label in card.select(".text-wrapper label"):
            if label.select_one("span") and label.select_one("strong"):
                fields[fold(_text(label.select_one("span")))] = _text(label.select_one("strong"))
        location = _text(card.select_one(".address"))
        price_text = _text(card.select_one(".price"))
        tags = " ".join(_text(tag) for tag in card.select(".tag-wrapper label"))
        metadata = {"property_type": fields.get("druh", ""),
                    "offer_type": fields.get("typ ponuky", ""),
                    "city": location.rsplit(",", 1)[-1].strip(),
                    "catalogue_title": _text(heading),
                    "price_text": price_text, "tags": tags, "detail_complete": "false"}
        if not all(metadata[key] for key in ("property_type", "offer_type", "city")):
            raise JozueParseError("Jozue card property type, offer type or location is missing")
        img = card.select_one(".img-wrapper img")
        listings.append(Listing(
            source="jozue", source_id=_source_id(url), url=url, title=_text(heading),
            description=clean_space(tags + " " + _text(card.select_one(".perex"))),
            price_eur=parse_jozue_price(price_text), location=location,
            image_url=urljoin(BASE_URL, img["src"]) if img and img.get("src") else None,
            metadata=metadata,
        ))
    pages = []
    for link in soup.select("#offers .pagination a[href]"):
        url = _source_url(link["href"], page_url)
        if not re.fullmatch(r"/prenajom-nehnutelnosti(?:/\d+/)?", urlsplit(url).path):
            raise JozueParseError("Unexpected Jozue pagination path")
        pages.append(url)
    return listings, pages


def parse_jozue_detail(page_html: str, listing: Listing) -> Listing:
    soup = BeautifulSoup(page_html, "html.parser")
    detail = soup.select_one("#offer-detail")
    if detail is None:
        raise JozueParseError("Jozue detail section is missing")
    heading = detail.select_one(".heading-wrapper h1")
    body = detail.select_one(".main > .wrap:not(.social):not(.detail-info):not(.location):not(.tags)")
    price_node = detail.select_one(".heading-wrapper .price")
    if not heading or not body or not _text(body) or price_node is None:
        raise JozueParseError("Jozue full description, heading or price is missing")
    fields = {}
    for item in detail.select(".base-info li.list-group-item"):
        value = item.select_one("span.label")
        if value:
            label = clean_space(" ".join(str(child) for child in item.children if isinstance(child, str)))
            fields[fold(label)] = _text(value)
    if not all(fields.get(key) for key in ("mesto", "druh", "typ", "pridane")):
        raise JozueParseError("Jozue detail property metadata or publication date is missing")
    published = parse_jozue_date(fields["pridane"])
    if not published:
        raise JozueParseError("Jozue publication date format is not recognized")
    metadata = dict(listing.metadata or {})
    metadata.update({"property_type": fields["druh"], "offer_type": fields["typ"],
                     "city": fields["mesto"], "price_text": _text(price_node),
                     "detail_complete": "true"})
    tags = " ".join(_text(tag) for tag in detail.select(".main > .image-wrapper .tag-wrapper label"))
    # A title/tag saying PRENAJATE overrides a stale 'Status: aktívne'.
    metadata["tags"] = clean_space(metadata.get("tags", "") + " " + tags)
    for item in detail.select(".detail-info .item"):
        if fold(_text(item.select_one("label"))) == "status":
            metadata["status"] = _text(item.select_one("strong"))
    image = detail.select_one("#imageGallery img[src]")
    price = _monthly_price(_text(body), metadata)
    return replace(listing, title=_text(heading), description=_text(body),
                   price_eur=price,
                   location=", ".join(v for v in (fields.get("ulica"), fields["mesto"]) if v),
                   published_at=published, metadata=metadata,
                   image_url=urljoin(BASE_URL, image["src"]) if image else listing.image_url)


class JozueClient:
    def __init__(self, timeout_seconds: int = 25, max_pages: int = 10,
                 request_delay_seconds: float = 1.0) -> None:
        self.timeout_seconds = timeout_seconds
        self.max_pages = max(1, max_pages)
        self.request_delay_seconds = max(0, request_delay_seconds)
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "FlatWatch/0.1 (+rental monitoring)",
                                     "Accept-Language": "sk-SK,sk;q=0.9"})

    def _get(self, url: str) -> str:
        time.sleep(self.request_delay_seconds)
        response = self.session.get(url, timeout=self.timeout_seconds)
        response.raise_for_status()
        final_url = _source_url(response.url, url)
        if final_url != _source_url(url, url):
            # A slug rename is fine, a redirect to the home page/another ad is not.
            if "/prenajom-nehnutelnosti" in url or _source_id(final_url) != _source_id(url):
                raise JozueParseError("Unexpected Jozue redirect")
        return response.text

    def fetch(self, url: str = RENTALS_URL) -> list[Listing]:
        try:
            pending = [_source_url(url, BASE_URL)]
            visited: set[str] = set()
            offers: dict[str, Listing] = {}
            while pending:
                page = pending.pop(0)
                if page in visited:
                    continue
                if len(visited) >= self.max_pages:
                    raise JozueParseError("Jozue page limit reached; catalogue was not fully scanned")
                listings, pages = parse_jozue_html(self._get(page), page)
                visited.add(page)
                for listing in listings:
                    offers.setdefault(listing.source_id, listing)
                pending.extend(link for link in pages if link not in visited and link not in pending)
            # Fetch all residential details, even stale/rented ones, for a complete audit.
            # Any failed detail aborts the scan, triggering the existing source-health alert.
            return [parse_jozue_detail(self._get(item.url), item)
                    if is_residential(item.metadata or {}) else item for item in offers.values()]
        finally:
            self.session.close()
