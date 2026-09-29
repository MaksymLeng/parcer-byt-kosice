from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
import hashlib
import re
import unicodedata


ROOM_PATTERNS = {
    1: (
        re.compile(r'(?<![\d,.])1\s*[-–]?\s*room\s+(?:flat|apartment)\b', re.I),
        re.compile(r"(?<![\d,.])1\s*[-–]?\s*izb\w*\.?", re.I),
        re.compile(r"\bjednoizb\w*", re.I),
        re.compile(r"(?<!\d)1\s*[-–]?\s*(?:комнат\w*|кімнат\w*)(?!\w)", re.I),
        re.compile(r"\b(?:однокомнат\w*|однокімнат\w*|studio|garsonk\w*)", re.I),
    ),
    2: (
        re.compile(r'(?<![\d,.])2\s*[-–]?\s*rooms?\b', re.I),
        re.compile(r"(?<![\d,.])2\s*[-–]?\s*izb\w*\.?", re.I),
        re.compile(r"\bdvojizb\w*", re.I),
        re.compile(r"(?<!\d)2\s*i\b", re.I),
        re.compile(r"\b(?:one|1)\s*[-–]?\s*bed(?:room)?\b", re.I),
        re.compile(r"(?<!\d)2\s*[-–]?\s*(?:комнат\w*|кімнат\w*)(?!\w)", re.I),
        re.compile(r"\b(?:двухкомнат\w*|двокімнат\w*)", re.I),
    ),
    3: (
        re.compile(r'(?<![\d,.])3\s*[-–]?\s*rooms?\b', re.I),
        re.compile(r"(?<![\d,.])3\s*[-–]?\s*izb\w*\.?", re.I),
        re.compile(r"\btrojizb\w*", re.I),
        re.compile(r"(?<!\d)3\s*i\b", re.I),
        re.compile(r"\b(?:two|2)\s*[-–]?\s*bed(?:room)?s?\b", re.I),
        re.compile(r"(?<!\d)3\s*[-–]?\s*(?:комнат\w*|кімнат\w*)(?!\w)", re.I),
        re.compile(r"\b(?:тр[её]хкомнат\w*|трикімнат\w*)", re.I),
    ),
}

PREFERRED_PATTERNS = {
    "Železníky": ("zelezn", "sid.lisko zelezn"),
    "Centrum": ("centrum", "centre", "historick", "hlavna", "aupark"),
    "Staré Mesto": ("stare mesto", "old town"),
}

WANTED_PATTERNS = (
    re.compile(r"\b(?:hľadám|hladam|hľadáme|hladame)\b", re.I),
    re.compile(r"\b(?:ищу|ищем|шукаю|шукаємо)\b", re.I),
    re.compile(r"\b(?:looking|searching)\s+for\b", re.I),
    re.compile(r"\bdopyt\b", re.I),
)

UNAVAILABLE_PATTERNS = (
    re.compile(r"\b(?:rezervovan[ýay]|reserved)\b", re.I),
    re.compile(r"\b(?:obsaden[ýay]|prenajat[ýay]|unavailable)\b", re.I),
)

APARTMENT_PATTERNS = (
    re.compile(r"\bbyt\w*", re.I),
    re.compile(r"\bapartm(?:ent|an)\w*", re.I),
    re.compile(r"\bflat\b", re.I),
    re.compile(r"\bквартир\w*", re.I),
    re.compile(r"\bжитло\b", re.I),
)

RENTAL_OFFER_PATTERNS = (
    re.compile(r"\b(?:prenaj|podnaj|najom)\w*", re.I),
    re.compile(r"\b(?:for|to)\s+rent\b", re.I),
    re.compile(r"\brent(?:ing|al)?\b", re.I),
    re.compile(r"\b(?:сдам|сда[её]тся|аренд\w*|здам|зда[єе]ться|оренд\w*|пропоную)\b", re.I),
    re.compile(r"(?:€|eur)\s*(?:/|za)?\s*(?:mesiac|mesac|month)", re.I),
)

SALE_PATTERNS = (
    re.compile(r"\b(?:predaj|predam)\w*", re.I),
    re.compile(r"\bfor\s+sale\b", re.I),
    re.compile(r"\b(?:продам|продаж\w*)\b", re.I),
)

ROOM_ONLY_PATTERNS = (
    re.compile(r"\b(?:prenaj\w*|ponuk\w*)\s+(?:samostatn\w+\s+)?izb(?:u|y)\b", re.I),
    re.compile(r"\bizba\s+na\s+prenaj\w*", re.I),
    re.compile(r"\broom\s+(?:for\s+rent|available)\b", re.I),
    re.compile(r"\b(?:roommate|spolubyv\w*)\b", re.I),
    re.compile(r"\b(?:сдам|здам|оренд\w*|аренд\w*)\s+(?:отдельную\s+|окрему\s+)?(?:комнату|кімнату)\b", re.I),
)

SHORT_TERM_PATTERNS = (
    re.compile(r'\b(?:посуточ\w*|подобов\w*)', re.I),
    re.compile(r"\b(?:kratkodob\w*|airbnb|booking)\b", re.I),
    re.compile(r"\b(?:na\s+noc|per\s+night|za\s+noc)\b", re.I),
)

# The object of "looking for" matters: a tenant is an offer; a flat is a request.
TENANT_SEARCH = re.compile(
    r'\b(?:hladam[e]?|hladame)\s+(?:slusn\w+\s+)?najom\w*|'
    r'\b(?:ищу|ищем|шукаю|шукаємо)\s+(?:квартирант\w*|жильц\w*|орендар\w*)|'
    r'\b(?:looking|searching)\s+for\s+(?:(?:a|new)\s+)?tenants?\b', re.I)
ROOMMATE_SEARCH = re.compile(
    r'\b(?:hladam|hladame)\s+(?:jedn\w+\s+|nov\w+\s+)?spolubyvaj\w*|'
    r'\b(?:ищу|ищем|шукаю|шукаємо|шукаєм)\s+(?:сосед\w*|сусід\w*|людину\s+на\s+підселення)|'
    r'\b(?:looking|searching)\s+for\s+(?:(?:a|new|one)\s+)?(?:roommates?|flatmates?)\b', re.I)
SHARED_DIRECT = re.compile(
    r'\b(?:ponuk\w*|prenaj\w*|na\s+prenajom)\s+(?:(?:na\s+prenajom|samostatn\w+|nepriechodn\w+|voln\w+|jedn\w+)\s+)*(?:izb[auy]|izieb|spaln\w*)\b|'
    r'\b(?:voln\w+|uvolnil\w*\s+sa)\s+(?:izb[auy]|miesto)\b|'
    r'\bizba\s+(?:na\s+prenajom|je\s+volna)\b|'
    r'\b(?:room|bed)\s+(?:for\s+rent|available)\b|'
    r'\broom\s+in\s+(?:a\s+)?(?:shared?|sharing)\s+flat\s+available\b|'
    r'\b(?:renting\s+out|available)\s+(?:a\s+|one\s+|single\s+|private\s+)?room\b|'
    r'\b(?:сдам|сда[её]тся|здам|здається|здається\s+в\s+оренду)\s+(?:(?:отдельн\w+|окрем\w+|вільн\w+)\s+)?(?:комнат\w*|кімнат\w*)|'
    r'\b(?:свободн\w*|вільн\w*)\s+(?:комнат\w*|кімнат\w*|место|місце)\b|'
    r'\b(?:есть|є)\s+(?:місце|место)\s+(?:на|для)\s+(?:подселени\w*|підселення)|'
    r'\b(?:предлага\w*|пропону\w*)\s+(?:подселени\w*|підселення)', re.I)
EXISTING_HOME = re.compile(
    r'\b(?:do|v)\s+(?:(?:\d+[- ]?izbov\w*|zariaden\w*|nas\w*)\s+)*(?:bytu|byte)\b|'
    r'\b(?:mame|mam)\s+(?:uz\s+)?byt\b|'
    r'\b(?:в|у)\s+(?:(?:наш\w*|мо\w*|\d+[- ]?кімнат\w*|\d+[- ]?комнат\w*)\s+)?квартир\w*|'
    r'\b(?:have|our|my|in\s+(?:a|the))\s+(?:an?\s+)?(?:apartment|flat)\b', re.I)
SEARCH_TOGETHER = re.compile(
    r'\b(?:look|search)\s+for\s+(?:an?\s+)?(?:apartment|flat)\s+together\b|'
    r'\b(?:spolu|spolocne)\s+(?:hladat|najst)\b|'
    r'\b(?:вместе|разом)\s+(?:искать|шукати|знайти|найти)\b', re.I)

FEMALE_OCCUPANT = re.compile(
    r'\b(?:pre|iba\s+pre|len\s+pre)\s+(?:(?:\d+|jednu|dve|slusne|pracujucu|studentku)\s+)*(?:dievca\w*|zen[uy]|studentk\w*)\b|'
    r'\b(?:hladam|hladame)\s+(?:(?:novu|jednu|slusnu)\s+)?(?:spolubyvajucu|dievca|studentku)\b|'
    r'\b(?:ищу|ищем|шукаю|шукаємо|для|только|лише|тільки)\s+(?:(?:одной|однієі|одну|двух|двох)\s+)?(?:девуш\w*|дівч\w*|женщин\w*|жін\w*|соседку|сусідку)\b|'
    r'\b(?:female|girls?|women|ladies)\s+only\b|'
    r'\bonly\s+(?:female|girls?|women|ladies)\b|'
    r'\bpreferuj\w*\s+(?:zensk\w*|dievca\w*)\b|'
    r'\b(?:for|prefer|prefers|preferably|need|seeking|like|looking\s+for)\s+(?:a\s+|one\s+)?(?:girl|woman|female)\b|'
    r'\bfemale\s+(?:roommate|flatmate|tenant)\b|'
    r'\bno\s+(?:men|boys|males)\b', re.I)


def female_only(text: str) -> bool:
    value = fold(text)
    # A statement of mixed eligibility overrides a mention of female occupants.
    value = re.sub(r'\b(?:male\s+or\s+female|female\s+or\s+male|men\s+and\s+women|chlapcov\s+aj\s+dievcat\w*|dievcat\w*\s+aj\s+chlapcov)\b', '', value)
    return bool(FEMALE_OCCUPANT.search(value))


def is_shared_offer(text: str) -> bool:
    value = fold(text)
    if SEARCH_TOGETHER.search(value):
        return False
    if not re.search(r'\b(?:byt\w*|izb\w*|izieb|spaln\w*|room\w*|flat\w*|apartment\w*|квартир\w*|комнат\w*|кімнат\w*|подселен\w*|підселен\w*)', value):
        return False
    # "If you have a spare room, contact me" is still a housing request.
    conditional = re.sub(r'\b(?:ak|if|если|якщо)\b[^.!?\n]*', '', value)
    existing = EXISTING_HOME.search(value) or re.search(r'\b(?:jedna\s+sa\s+o\s+byt|it\s+is\s+(?:an?\s*)?apartment)\b', value)
    return bool(SHARED_DIRECT.search(conditional) or (ROOMMATE_SEARCH.search(value) and existing))


def is_wanted_post(text: str) -> bool:
    value = fold(text)
    if SEARCH_TOGETHER.search(value):
        return True
    if re.search(r'\b(?:(?:i\s+am|we\s+are)\s+offering|(?:we|i)\s+offer)\s+(?:for\s+rent|a\s+.*?apartment)|\bponuk\w*\s+(?:na|do)\s+(?:dlhodob\w*\s+)?prenaj\w*|\bprenajmem\b', value):
        return False
    value = TENANT_SEARCH.sub('', value)
    if is_shared_offer(text):
        value = ROOMMATE_SEARCH.sub('', value)
    return bool(re.search(r'\bhlada\s+(?:byt|ubytovanie|izbu|prenajom)\b', value)) or any(pattern.search(value) for pattern in WANTED_PATTERNS)

UTILITIES_INCLUDED_PATTERNS = (
    re.compile(r'\b(?:zahrna|zahrnute|zahŕňa|zahrnuté)\s+(?:vsetky\s+|platby\s+za\s+)?energi', re.I),
    re.compile(r'\b(?:rent\s+and\s+utilities|all[ -]inclusive|bills\s+(?:and\s+wifi\s+)?included)\b', re.I),
    re.compile(r"\bvr[aá]tane\s+(?:energi|m[eé]di)", re.I),
    re.compile(r"\bv\s+cene\s+(?:s[uú]\s+)?(?:energi|m[eé]di|internet)", re.I),
    re.compile(r"\b(?:energi\w*|m[eé]di\w*)\s+(?:s[uú]\s+)?v\s+cene\b", re.I),
    re.compile(r"\binclud(?:es|ing|ed)\s+utilit", re.I),
    re.compile(r"\b(?:коммун\w*|комун\w*)\s+включ", re.I),
)

UTILITIES_EXTRA_PATTERNS = (
    re.compile(r"(?:\+|plus)\s*(?:energi|m[eé]di|poplat|utilit)", re.I),
    re.compile(r"\bbez\s+(?:energi|m[eé]di|poplat)", re.I),
    re.compile(r"\b(?:energi\w*|m[eé]di\w*|poplat\w*|utilit\w*)\s+(?:navyše|extra)", re.I),
    re.compile(r"(?:\+|плюс|додатково)\s*(?:коммун|комун)", re.I),
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def fold(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value)
    return "".join(char for char in normalized if not unicodedata.combining(char)).casefold()


def clean_space(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


@dataclass(slots=True, frozen=True)
class Listing:
    source: str
    source_id: str
    url: str
    title: str
    description: str
    price_eur: int | None
    location: str
    published_at: str | None = None
    image_url: str | None = None
    preferred_area: str | None = None
    first_seen_at: str | None = None
    # Structured source fields, kept separately from free-form advertisement text.
    metadata: dict[str, str] | None = None

    @property
    def text(self) -> str:
        # Jozue can put '+Energie' / price units only in a badge or price label.
        labels = ""
        if self.source == "jozue" and self.metadata:
            labels = " ".join(self.metadata.get(key, "") for key in ("tags", "price_text"))
            if self.metadata.get("utilities_included") == "true":
                labels += " vrátane energií"
        return clean_space(f"{self.title} {self.description} {self.location} {labels}")


def find_prices(text: str) -> list[int]:
    normalized = text.replace("\u00a0", " ")
    candidates: list[int] = []
    for match in re.finditer(
        r"(?<![\d.])(\d{1,2}[ .]\d{3}|\d{2,5})(?:[,.](?:00|-))?\s*(?:€|eur(?:o)?)(?!\w)",
        normalized,
        re.I,
    ):
        value = int(re.sub(r'[ .]', '', match.group(1)))
        if 50 <= value <= 5_000:
            candidates.append(value)
    return candidates


def find_price(text: str) -> int | None:
    # Prefer explicit monthly rent over a deposit mentioned first.
    folded = fold(text)
    for match in re.finditer(r'\b(?:najom(?:ne)?|rent|аренда|оренда)\s*(?:je|is|:|=)?\s*(\d{2,4})(?:[,.](?:00|-))?\s*(?:€|eur)', folded):
        return int(match[1])
    before = re.search(r'(?:€|eur)\s*(\d{1,2}[, ]\d{3}|\d{2,5})(?:\.00)?', folded)
    if before:
        return int(re.sub('[, ]', '', before[1]))
    candidates = find_prices(text)
    return candidates[0] if candidates else None


def find_preferred_area(text: str) -> str | None:
    haystack = fold(text)
    # A shopping centre in Furča does not make the apartment a city-centre listing.
    haystack = re.sub(r'\b(?:obchodne|nakupne)\s+centrum\b', '', haystack)
    for area, needles in PREFERRED_PATTERNS.items():
        if any(needle in haystack for needle in needles):
            return area
    return None


def find_rooms(text: str) -> int | None:
    for rooms, patterns in ROOM_PATTERNS.items():
        if any(pattern.search(text) for pattern in patterns):
            return rooms
    return None


def utilities_status(text: str) -> str:
    text = fold(text)
    if any(pattern.search(text) for pattern in UTILITIES_INCLUDED_PATTERNS):
        return "included"
    if any(pattern.search(text) for pattern in UTILITIES_EXTRA_PATTERNS):
        return "extra"
    return "unknown"


def facebook_offer_reason(text: str) -> str | None:
    folded = fold(text)
    if any(pattern.search(folded) for pattern in ROOM_ONLY_PATTERNS):
        return "room_only"
    if any(pattern.search(folded) for pattern in SHORT_TERM_PATTERNS):
        return "short_term"
    if not any(pattern.search(folded) for pattern in APARTMENT_PATTERNS):
        return "not_apartment"
    rental_offer = any(pattern.search(folded) for pattern in RENTAL_OFFER_PATTERNS) or bool(TENANT_SEARCH.search(folded))
    sale_offer = any(pattern.search(folded) for pattern in SALE_PATTERNS)
    if sale_offer and not rental_offer:
        return "sale_post"
    if not rental_offer:
        return "not_rental_offer"
    return None


def listing_fingerprint(listing: Listing) -> str:
    value = fold(listing.title)
    value = re.sub(
        r"(?<!\w)\d+\s*(?:мин|хв|min|ч|год|h|дн|д|d|нед|тиж|w)\.?(?!\w)",
        " ",
        value,
        flags=re.I,
    )
    value = re.sub(
        r"\b(?:показать перевод|see translation|zobrazit preklad|отправить сообщение)\b",
        " ",
        value,
        flags=re.I,
    )
    normalized = " ".join(re.findall(r"[\w€]+", value, flags=re.UNICODE))
    if len(normalized) < 60:
        fallback = fold(listing.description)
        fallback = re.sub(
            r"(?<!\w)\d+\s*(?:мин|хв|min|ч|год|h|дн|д|d|нед|тиж|w)\.?(?!\w)",
            " ",
            fallback,
            flags=re.I,
        )
        normalized = " ".join(re.findall(r"[\w€]+", fallback, flags=re.UNICODE))[:700]
    normalized = f"{normalized[:700]}|{listing.price_eur or 0}"
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def total_with_known_utilities(base_price: int, text: str) -> int:
    folded = fold(text)
    parking = re.search(r'\+\s*(\d{2,3})\s*€\s*(?:parkovac\w*|parking)', folded)
    if parking and not re.search(r'\b(?:optional|volitel\w*|mozn\w*)\b', folded):
        base_price += int(parking[1])
    if utilities_status(text) == "included":
        return base_price
    patterns = (
        r"(?:energie|media|poplatky|utilities|коммун\w*|комун\w*)[^\d€]{0,30}(\d{2,3})\s*€",
        r"(\d{2,3})\s*€[^\d]{0,30}(?:energie|media|poplatky|utilities|коммун\w*|комун\w*)",
    )
    for pattern in patterns:
        match = re.search(pattern, folded, re.I)
        if match:
            extra = int(match.group(1))
            if 20 <= extra <= 500 and extra != base_price:
                return base_price + extra
    return base_price


def listing_age_hours(listing: Listing) -> float | None:
    if not listing.published_at:
        return None
    value = listing.published_at.strip()
    try:
        if re.fullmatch(r"\d{1,2}\.\d{1,2}\.\d{4}", value):
            published = datetime.strptime(value, "%d.%m.%Y").replace(tzinfo=timezone.utc)
        else:
            published = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if published.tzinfo is None:
                published = published.replace(tzinfo=timezone.utc)
        age = datetime.now(timezone.utc) - published.astimezone(timezone.utc)
        return max(0.0, age.total_seconds() / 3600)
    except ValueError:
        return None


def matches_filters(listing: Listing, filters: dict) -> tuple[bool, str, Listing]:
    text = listing.text
    folded = fold(text)
    city = fold(str(filters.get("city", "Košice")))

    metadata = listing.metadata or {}
    if listing.source == 'bazos' and re.search(re.escape(city) + r'\s*-\s*okolie\b', fold(listing.location)):
        return False, 'outside_city', listing
    if listing.source == 'nehnutelnosti':
        if metadata.get('availability') not in {'https://schema.org/InStock', ''}:
            return False, 'unavailable', listing
        if not re.fullmatch(r'[23][ -]izbovy byt', fold(metadata.get('property_type', listing.title))):
            return False, 'wrong_rooms', listing
    jozue_room = listing.source == "jozue" and fold(metadata.get("property_type", "")) in {"izba", "izby", "spolubyvanie"}
    if listing.source == "jozue":
        town = fold(metadata.get("city", ""))
        if not re.fullmatch(re.escape(city) + r"(?:\s*-\s*(?!okolie\b).+)?", town):
            return False, "outside_city", listing
        if fold(metadata.get("offer_type", "")) not in {"prenajom", "podnajom"}:
            return False, "not_rental_offer", listing
        availability = fold(" ".join((listing.title, metadata.get("catalogue_title", ""),
                                      metadata.get("tags", ""), metadata.get("status", ""))))
        if re.search(r"\b(?:prenajat\w*|rezervovan\w*|obsaden\w*|predan\w*|neaktiv\w*)\b", availability):
            return False, "unavailable", listing
        kind = fold(metadata.get("property_type", ""))
        if not jozue_room and not re.fullmatch(r"(?:\d+[- ]izbovy byt|garsonka)(?:\s*[-–]\s*novostavba)?", kind):
            return False, "not_apartment", listing
        price_basis = fold(metadata.get("price_text", "") + " " + metadata.get("tags", ""))
        if re.search(r"\b(?:noc|night|den|day)\b|/\s*m[²2]", price_basis):
            return False, "short_term", listing
        if metadata.get("detail_complete") != "true":
            return False, "incomplete_post", listing
        if listing.price_eur is None:
            return False, "price_missing", listing

    # Do not notify on a truncated body: gender restrictions or extra costs may be hidden.
    if listing.source == 'facebook' and re.search(r'(?:Ещё|Еще|See more|Zobraziť viac|Показати більше|Докладніше)\s*$', listing.description, re.I):
        return False, 'incomplete_post', listing

    # Dedicated Košice Facebook groups are accepted even when a post omits the city.
    if listing.source != "facebook" and city not in folded:
        return False, "outside_city", listing
    if listing.source == 'facebook':
        body = fold(listing.title + ' ' + listing.description)
        kosice = re.search(r'\b(?:kosic\w*|кошиц\w*|кошіч\w*|кошіц\w*)', body)
        other_city = re.search(r'\b(?:bratislava|bratislave|presov|presove|poprad|poprade|michalovce|trnava|trnave|nitra|nitre|братислав\w*|прешов\w*)\b', body)
        if other_city and not kosice:
            return False, 'outside_city', listing

    shared = jozue_room or is_shared_offer(text)
    if is_wanted_post(text):
        return False, "wanted_post", listing

    if filters.get('occupant_gender') == 'male' and female_only(text):
        return False, 'female_only', listing

    availability_text = re.sub(r'\breserved\s+parking\s+(?:space|spot)\b', '', text, flags=re.I)
    availability_text = re.sub(r'\b(?:private\s+)?reserved\s+parking\b', '', availability_text, flags=re.I)
    if any(pattern.search(availability_text) for pattern in UNAVAILABLE_PATTERNS):
        return False, "unavailable", listing

    if shared and not filters.get('shared_housing', {}).get('enabled', False):
        return False, 'room_only', listing

    if (shared or listing.source == "jozue") and any(pattern.search(folded) for pattern in SHORT_TERM_PATTERNS):
        return False, 'short_term', listing

    if listing.source == "facebook" and not shared:
        offer_reason = facebook_offer_reason(text)
        if offer_reason:
            return False, offer_reason, listing

    configured_rooms = filters.get("rooms", [2])
    if isinstance(configured_rooms, int):
        configured_rooms = [configured_rooms]
    rooms = find_rooms(metadata.get("property_type", "")) if listing.source == "jozue" else find_rooms(text)
    if not shared and rooms not in {int(value) for value in configured_rooms}:
        return False, "wrong_rooms", listing

    age_hours = listing_age_hours(listing)
    max_age_hours = float(filters.get("max_age_hours", 48))
    if age_hours is not None and age_hours > max_age_hours:
        return False, "stale_listing", listing
    if listing.source in {"facebook", "jozue", "nehnutelnosti"} and age_hours is None:
        return False, "published_time_missing", listing

    minimum = int(filters.get("price_min", 0))
    maximum_by_rooms = filters.get("price_max_by_rooms", {})
    maximum = int(maximum_by_rooms.get(str(rooms), filters.get("price_max", 100_000)))
    if shared:
        minimum = int(filters['shared_housing'].get('price_min', 0))
        maximum = int(filters['shared_housing'].get('price_max', filters.get('price_max', 750)))
    price = listing.price_eur
    if price is None:
        candidates = find_prices(text)
        price = next((value for value in candidates if minimum <= value <= maximum), None)
        if price is None and candidates:
            price = candidates[0]
        listing = replace(listing, price_eur=price)
    if price is None:
        return False, "price_missing", listing

    adjusted_price = total_with_known_utilities(price, text)
    known_utilities_added = adjusted_price != price
    if adjusted_price != price:
        price = adjusted_price
        listing = replace(listing, price_eur=price)

    if price >= maximum and utilities_status(text) == "extra" and not known_utilities_added:
        return False, "utilities_extra", listing

    if not minimum <= price <= maximum:
        return False, "price_outside_range", listing

    preferred_area = find_preferred_area(text)
    listing = replace(
        listing,
        preferred_area=preferred_area,
        first_seen_at=listing.first_seen_at or utc_now(),
    )
    return True, "matched", listing
