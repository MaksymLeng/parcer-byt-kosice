import unittest
from datetime import datetime, timedelta, timezone

from flatwatch.bazos import parse_bazos_detail, parse_bazos_html
from flatwatch.core import (
    Listing,
    facebook_offer_reason,
    find_price,
    find_rooms,
    listing_fingerprint,
    matches_filters,
    utilities_status,
)
from flatwatch.facebook import extract_post_id, parse_facebook_published


BAZOS_FIXTURE = """
<div class="inzeraty inzeratyflex">
  <div class="inzeratynadpis">
    <a href="/inzerat/195041452/atraktivny-byt.php"><img src="https://www.bazos.sk/img/1t/452/195041452.jpg" class="obrazek"></a>
    <h2 class=nadpis><a href="/inzerat/195041452/atraktivny-byt.php">Atraktívny 2 izbový byt, blízko Centra</a></h2>
    <span class=velikost10> - [29.8. 2026]</span><br>
    <div class=popis>Prenájom zariadeného bytu v Starom Meste.</div>
  </div>
  <div class="inzeratycena"><b><span translate="no">750 €</span></b></div>
  <div class="inzeratylok">Košice<br>040 01</div>
</div>
"""


class BazosTests(unittest.TestCase):
    def test_parse_listing(self):
        listings = parse_bazos_html(BAZOS_FIXTURE)
        self.assertEqual(len(listings), 1)
        listing = listings[0]
        self.assertEqual(listing.source_id, "195041452")
        self.assertEqual(listing.price_eur, 750)
        self.assertEqual(listing.location, "Košice 040 01")
        self.assertEqual(listing.published_at, "29.8.2026")
        self.assertIn("reality.bazos.sk/inzerat/", listing.url)

    def test_parse_detail(self):
        listing = parse_bazos_html(BAZOS_FIXTURE)[0]
        enriched = parse_bazos_detail(
            '<div class=popisdetail>V cene sú energie a internet.<br>Cena: 750 €/mesiac</div>',
            listing,
        )
        self.assertIn("energie a internet", enriched.description)


class FilterTests(unittest.TestCase):
    filters = {
        "city": "Košice",
        "rooms": [2, 3],
        "price_min": 500,
        "price_max": 750,
        "price_max_by_rooms": {"2": 750, "3": 800},
    }

    def make_listing(self, title: str, price: int | None = 700) -> Listing:
        return Listing("bazos", "1", "https://example.test/1", title, "", price, "Košice")

    def test_accepts_two_room_preferred(self):
        accepted, _, listing = matches_filters(
            self.make_listing("2-izbový byt, Staré Mesto"), self.filters
        )
        self.assertTrue(accepted)
        self.assertEqual(listing.preferred_area, "Staré Mesto")

    def test_rejects_wrong_room_count(self):
        accepted, reason, _ = matches_filters(self.make_listing("1-izbový byt"), self.filters)
        self.assertFalse(accepted)
        self.assertEqual(reason, "wrong_rooms")

    def test_rejects_price(self):
        accepted, reason, _ = matches_filters(
            self.make_listing("2 izbovy byt", price=800), self.filters
        )
        self.assertFalse(accepted)
        self.assertEqual(reason, "price_outside_range")

    def test_accepts_three_room_at_800(self):
        accepted, _, _ = matches_filters(
            self.make_listing("3-izbový byt", price=800), self.filters
        )
        self.assertTrue(accepted)

    def test_rejects_two_room_at_800(self):
        accepted, reason, _ = matches_filters(
            self.make_listing("2-izbový byt", price=800), self.filters
        )
        self.assertFalse(accepted)
        self.assertEqual(reason, "price_outside_range")

    def test_multilingual_room_detection(self):
        self.assertEqual(find_rooms("Трёхкомнатная квартира"), 3)
        self.assertEqual(find_rooms("2-кімнатна квартира"), 2)
        self.assertEqual(find_rooms("1 bedroom apartment"), 2)
        self.assertEqual(find_rooms("2 bedroom apartment"), 3)
        self.assertEqual(find_rooms("Prenajmem 2i byt"), 2)

    def test_extracts_facebook_style_price(self):
        self.assertEqual(find_price("Cena je 700 € vrátane energií"), 700)

    def test_rejects_wanted_post(self):
        accepted, reason, _ = matches_filters(
            self.make_listing("Hľadám 2-izbový byt do 700 €", price=None), self.filters
        )
        self.assertFalse(accepted)
        self.assertEqual(reason, "wanted_post")

    def test_rejects_reserved_listing(self):
        accepted, reason, _ = matches_filters(
            self.make_listing("REZERVOVANÝ - 3-izbový byt", price=750), self.filters
        )
        self.assertFalse(accepted)
        self.assertEqual(reason, "unavailable")

    def test_chooses_rent_in_range_instead_of_deposit(self):
        accepted, _, listing = matches_filters(
            self.make_listing("2-izbový byt, depozit 1400 €, nájom 700 €", price=None),
            self.filters,
        )
        self.assertTrue(accepted)
        self.assertEqual(listing.price_eur, 700)

    def test_rejects_upper_limit_when_utilities_are_extra(self):
        accepted, reason, _ = matches_filters(
            self.make_listing("3-izbový byt, cena 800 € + energie", price=800), self.filters
        )
        self.assertFalse(accepted)
        self.assertEqual(reason, "utilities_extra")

    def test_adds_known_utilities_to_total(self):
        accepted, reason, listing = matches_filters(
            self.make_listing("2-izbový byt, nájom 700 €, energie 100 €", price=700),
            self.filters,
        )
        self.assertFalse(accepted)
        self.assertEqual(reason, "price_outside_range")
        self.assertEqual(listing.price_eur, 800)

    def test_utilities_included(self):
        self.assertEqual(utilities_status("V cene sú energie a internet"), "included")

    def test_rejects_old_listing_even_if_it_just_appeared_in_search(self):
        published = (datetime.now(timezone.utc) - timedelta(days=3)).strftime("%-d.%-m.%Y")
        listing = self.make_listing("3-izbový byt", price=800)
        listing = Listing(
            listing.source,
            listing.source_id,
            listing.url,
            listing.title,
            listing.description,
            listing.price_eur,
            listing.location,
            published_at=published,
        )
        accepted, reason, _ = matches_filters(listing, self.filters)
        self.assertFalse(accepted)
        self.assertEqual(reason, "stale_listing")


class FacebookTests(unittest.TestCase):
    def test_extract_post_id(self):
        self.assertEqual(
            extract_post_id("https://www.facebook.com/groups/example/posts/123456789/"),
            "123456789",
        )

    def test_parse_relative_time(self):
        now = datetime(2026, 9, 2, 12, 0, tzinfo=timezone.utc)
        self.assertEqual(
            parse_facebook_published("Jan Novak 2 ч. · Ponúkam byt", now),
            "2026-09-02T10:00:00+00:00",
        )

    def test_rejects_facebook_post_without_reliable_publish_time(self):
        listing = Listing(
            "facebook",
            "42",
            "https://www.facebook.com/groups/example/posts/42/",
            "Ponúkam do prenájmu 2-izbový byt",
            "Cena 700 €",
            700,
            "Košice",
        )
        accepted, reason, _ = matches_filters(listing, FilterTests.filters)
        self.assertFalse(accepted)
        self.assertEqual(reason, "published_time_missing")

    def test_accepts_whole_apartment_offer(self):
        self.assertIsNone(
            facebook_offer_reason("Ponúkam do prenájmu zariadený 2-izbový byt v Košiciach")
        )

    def test_rejects_room_only_offer(self):
        self.assertEqual(
            facebook_offer_reason("Ponúkam samostatnú izbu v 3-izbovom byte"),
            "room_only",
        )

    def test_rejects_sale_and_general_discussion(self):
        self.assertEqual(facebook_offer_reason("Predám 2-izbový byt"), "sale_post")
        self.assertEqual(
            facebook_offer_reason("Aká je dobrá cena za 2-izbový byt?"),
            "not_rental_offer",
        )

    def test_accepts_ukrainian_apartment_offer(self):
        self.assertIsNone(
            facebook_offer_reason("Здається в оренду 2-кімнатна квартира в Кошице")
        )

    def test_fingerprint_ignores_changing_facebook_age(self):
        first = Listing(
            "facebook",
            "1",
            "https://example.test/1",
            "Laura 11 ч. · Ponúkam do prenájmu 2-izbový byt",
            "Laura 11 ч. · Ponúkam do prenájmu 2-izbový byt za 700 €",
            700,
            "Košice",
        )
        second = Listing(
            "facebook",
            "2",
            "https://example.test/2",
            "Laura 12 ч. · Ponúkam do prenájmu 2-izbový byt",
            "Laura 12 ч. · Ponúkam do prenájmu 2-izbový byt za 700 €",
            700,
            "Košice",
        )
        self.assertEqual(listing_fingerprint(first), listing_fingerprint(second))


if __name__ == "__main__":
    unittest.main()
