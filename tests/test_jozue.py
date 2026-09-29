import json
import sqlite3
import tempfile
import unittest
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from flatwatch.app import build_parser, collect, enabled_sources, main, run_check
from flatwatch.core import Listing, matches_filters, utilities_status
from flatwatch.health import SourceHealth
from flatwatch.history import collect_history
from flatwatch.jozue import (
    JozueClient, JozueParseError, RENTALS_URL, parse_jozue_date,
    parse_jozue_detail, parse_jozue_html, parse_jozue_price,
)
from flatwatch.storage import ListingStore, SCHEMA
from flatwatch.telegram import TelegramNotifier


# Minimized, representative HTML from the public catalogue and detail page.
CARD = '''<div class="offer full grid-view">
<div class="img-wrapper"><div class="tag-wrapper"><label>+Energie</label></div>
<a href="/2-izbovy-byt-1014555"><img src="/static/photo.jpg"></a></div>
<div class="text-wrapper"><a href="/2-izbovy-byt-1014555"><h2>2 IZBOVY BYT NA PRENAJOM</h2></a>
<p class="address">Gagarinova, Košice - mestská časť Nad jazerom</p>
<p class="perex">Ponúkame na prenájom byt...</p>
<label><span>Typ ponuky</span><strong>prenájom</strong></label>
<label><span>Druh</span><strong>2-izbový byt</strong></label>
<label class="price">700,- €</label></div></div>'''


def catalogue(card=CARD, pagination=""):
    return '<section id="offers"><div id="ponuky_grid">' + card + '</div><ul class="pagination">' + pagination + '</ul></section>'


DETAIL = '''<section id="offer-detail">
<div class="heading-wrapper"><h1>2 IZBOVY BYT NA PRENAJOM</h1><label class="price">700,- €</label></div>
<div class="main panel"><div class="image-wrapper"><div class="tag-wrapper"><label>exkluzívne</label></div>
<ul id="imageGallery"><li><img src="/static/full.jpg"></li></ul></div>
<div class="wrap"><h3>2-izbový byt na prenájom</h3><p>Ponúkam na prenájom byt.<br>Depozit 1400 €.</p></div>
<div class="wrap social">NEVER INCLUDE SHARE WIDGET</div>
<div class="wrap detail-info"><div class="item"><label>Status</label><strong>aktívne</strong></div></div>
</div><div class="base-info"><ul>
<li class="list-group-item">Mesto <span class="label">Košice - mestská časť Nad jazerom</span></li>
<li class="list-group-item">Ulica <span class="label">Gagarinova</span></li>
<li class="list-group-item">Druh <span class="label">2-izbový byt</span></li>
<li class="list-group-item">Typ <span class="label">prenájom</span></li>
<li class="list-group-item">Pridané <span class="label">3. júl 2026 11:09 hod</span></li>
</ul></div><div class="featured-offers"><h1>PRENAJATE unrelated apartment</h1><label class="price">300 €</label></div>
</section>'''


def candidate(**changes):
    item = parse_jozue_detail(DETAIL, parse_jozue_html(catalogue())[0][0])
    return replace(item, **{"published_at": datetime.now(timezone.utc).isoformat(), **changes})


class JozueParserTests(unittest.TestCase):
    def test_catalogue_fields_and_pagination(self):
        listings, pages = parse_jozue_html(catalogue(pagination='<li><a href="/prenajom-nehnutelnosti/1/">2</a></li>'))
        item = listings[0]
        self.assertEqual(item.source_id, "1014555")
        self.assertEqual(item.price_eur, 700)
        self.assertEqual(item.metadata["property_type"], "2-izbový byt")
        self.assertEqual(item.metadata["city"], "Košice - mestská časť Nad jazerom")
        self.assertEqual(item.metadata["detail_complete"], "false")
        self.assertEqual(pages, [RENTALS_URL + "/1/"])
        self.assertIsNone(item.published_at)

    def test_full_detail_date_price_and_no_recommended_offers(self):
        item = parse_jozue_detail(DETAIL, parse_jozue_html(catalogue())[0][0])
        self.assertEqual(item.published_at, "2026-07-03T09:09:00+00:00")
        self.assertEqual(item.price_eur, 700)
        self.assertIn("Depozit 1400", item.description)
        self.assertNotIn("unrelated", item.text)
        self.assertNotIn("SHARE WIDGET", item.text)
        self.assertTrue(item.image_url.endswith("/static/full.jpg"))
        self.assertEqual(item.metadata["detail_complete"], "true")
        self.assertEqual(utilities_status(item.text), "extra")

    def test_price_formats_and_not_deposit(self):
        for value, expected in [("700,- €", 700), ("1.100,- €", 1100), ("1 100,00 €", 1100),
                                ("750,50 €", 751), ("800 EUR", 800), ("Cena v RK", None),
                                ("Dohodou", None), ("11,25 €/m2/mes.", 12)]:
            with self.subTest(value=value):
                self.assertEqual(parse_jozue_price(value), expected)

    def test_real_price_discrepancy_prefers_larger_rent_not_deposit(self):
        detail = DETAIL.replace("700,- €", "670,- €").replace("Depozit 1400 €.", "Depozit 1400 €. Nájomné s energiami vo výške 680,-eur.")
        item = parse_jozue_detail(detail, parse_jozue_html(catalogue())[0][0])
        self.assertEqual(item.price_eur, 680)
        self.assertIn("670", item.metadata["price_note"])
        self.assertIn("680", item.metadata["price_note"])
        self.assertEqual(utilities_status(item.text), "included")

    def test_explicit_body_rent_above_budget_is_not_missed(self):
        detail = DETAIL.replace("Depozit 1400 €.", "Depozit 1400 €. Výška nájomného je 900,-eur.")
        item = parse_jozue_detail(detail, parse_jozue_html(catalogue())[0][0])
        self.assertEqual(item.price_eur, 900)

    def test_date_winter_and_bad_format(self):
        self.assertEqual(parse_jozue_date("15. január 2026 12:30 hod"), "2026-01-15T11:30:00+00:00")
        for value in ("yesterday", "31. február 2026 10:00 hod", ""):
            self.assertIsNone(parse_jozue_date(value))

    def test_all_month_names(self):
        for index, month in enumerate(("január", "február", "marec", "apríl", "máj", "jún",
                                      "júl", "august", "september", "október", "november", "december"), 1):
            self.assertIn(f"2026-{index:02d}-03", parse_jozue_date(f"3. {month} 2026 11:09 hod"))

    def test_changed_markup_is_error_not_empty_success(self):
        for page in ("<html>Blocked</html>", catalogue(""), catalogue(CARD.replace("<h2>", "<h4>").replace("</h2>", "</h4>"))):
            with self.assertRaises(JozueParseError):
                parse_jozue_html(page)
        self.assertEqual(parse_jozue_html(catalogue("Žiadne ponuky"))[0], [])

    def test_missing_full_detail_or_date_fails_closed(self):
        listing = parse_jozue_html(catalogue())[0][0]
        for detail in ("<html>404</html>", DETAIL.replace("Pridané", "Unknown"),
                       DETAIL.replace("3. júl 2026 11:09 hod", "yesterday"), DETAIL.replace('class="wrap"', 'class="new-layout"')):
            with self.assertRaises(JozueParseError):
                parse_jozue_detail(detail, listing)

    def test_offsite_and_unexpected_pagination_blocked(self):
        for href in ("https://example.org/payload", "/hladanie?sc=foo"):
            with self.assertRaises(JozueParseError):
                parse_jozue_html(catalogue(pagination=f'<a href="{href}">2</a>'))

    def test_three_pages_and_repeated_ids(self):
        client = JozueClient(request_delay_seconds=0)
        pages = {
            RENTALS_URL: catalogue(pagination='<a href="/prenajom-nehnutelnosti/1/">2</a><a href="/prenajom-nehnutelnosti/2/">3</a>'),
            RENTALS_URL + "/1/": catalogue(CARD.replace("1014555", "202"), '<a href="/prenajom-nehnutelnosti">1</a>'),
            RENTALS_URL + "/2/": catalogue(),
        }
        with patch.object(client, "_get", side_effect=lambda url: pages.get(url, DETAIL)) as get:
            items = client.fetch()
        self.assertEqual([item.source_id for item in items], ["1014555", "202"])
        self.assertEqual(get.call_count, 5)

    def test_page_limit_raises_instead_of_truncating(self):
        client = JozueClient(max_pages=1, request_delay_seconds=0)
        with patch.object(client, "_get", return_value=catalogue(pagination='<a href="/prenajom-nehnutelnosti/1/">2</a>')):
            with self.assertRaises(JozueParseError):
                client.fetch()

    def test_http_failure_propagates(self):
        client = JozueClient(request_delay_seconds=0)
        client.session.get = Mock(side_effect=TimeoutError("source down"))
        with self.assertRaises(TimeoutError):
            client.fetch()

    def test_redirect_to_home_is_error_but_slug_rename_is_allowed(self):
        client = JozueClient(request_delay_seconds=0)
        response = Mock(url="https://www.jozue.sk/", text="home")
        client.session.get = Mock(return_value=response)
        with self.assertRaises(JozueParseError):
            client._get(RENTALS_URL + "/1/")
        response.url = "https://www.jozue.sk/renamed-1014555"
        response.text = DETAIL
        self.assertEqual(client._get("https://www.jozue.sk/old-title-1014555"), DETAIL)


class JozueFilterTests(unittest.TestCase):
    def setUp(self):
        self.filters = json.loads(Path("config.json").read_text())["filters"]

    def check(self, item, reason="matched"):
        self.assertEqual(matches_filters(item, self.filters)[:2], (reason == "matched", reason))

    def test_fresh_two_room_whole_flat(self):
        self.check(candidate())
        self.check(candidate(price_eur=800, metadata={**candidate().metadata, "tags": ""}))

    def test_reserved_and_rented_title_or_badge_overrides_active(self):
        for label in ("PRENAJATE", "prenajaté", "Prenajaty", "REZERVOVANE", "rezervované", "obsadené"):
            with self.subTest(label=label):
                item = candidate(title=f"{label}-2 IZBOVY BYT NA PRENAJOM")
                self.check(item, "unavailable")
                self.check(candidate(metadata={**item.metadata, "tags": label}), "unavailable")

    def test_old_and_missing_publication_time(self):
        self.check(candidate(published_at=(datetime.now(timezone.utc) - timedelta(hours=49)).isoformat()), "stale_listing")
        self.check(candidate(published_at=None), "published_time_missing")

    def test_rented_catalogue_title_survives_unmarked_detail_title(self):
        card = parse_jozue_html(catalogue(CARD.replace('>2 IZBOVY', '>PRENAJATE-2 IZBOVY')))[0][0]
        item = parse_jozue_detail(DETAIL, card)
        self.check(item, "unavailable")

    def test_city_is_property_location_not_agency_address(self):
        for town in ("Košice-okolie", "Haniska", "Čaňa", "Valaliky", "Sokoľany"):
            self.check(candidate(metadata={**candidate().metadata, "city": town}), "outside_city")

    def test_commercial_property_even_if_mentions_rooms(self):
        self.check(candidate(metadata={**candidate().metadata, "property_type": "Kancelárie"}), "not_apartment")

    def test_structured_room_count_not_marketing_text(self):
        for kind in ("1-izbový byt", "4-izbový byt", "Garsónka"):
            self.check(candidate(metadata={**candidate().metadata, "property_type": kind}), "wrong_rooms")
        self.check(candidate(metadata={**candidate().metadata, "property_type": "3-izbový byt"}))
        self.check(candidate(metadata={**candidate().metadata, "property_type": "2-izbový byt - novostavba"}))

    def test_room_offer_and_gender(self):
        item = candidate(title="Izba na prenájom", price_eur=300,
                         metadata={**candidate().metadata, "property_type": "Izba"})
        self.check(item)
        self.check(replace(item, description="Ponúkam izbu len pre dievča"), "female_only")

    def test_unknown_price_is_not_replaced_by_deposit(self):
        self.check(candidate(price_eur=None, description="Depozit 700 €"), "price_missing")

    def test_utilities_badge_preserved(self):
        self.check(candidate(price_eur=800), "utilities_extra")

    def test_price_per_night_not_treated_as_monthly(self):
        self.check(candidate(metadata={**candidate().metadata, "tags": "Cena/Noc/osoba"}), "short_term")

    def test_sale_and_incomplete_detail(self):
        self.check(candidate(metadata={**candidate().metadata, "offer_type": "predaj"}), "not_rental_offer")
        self.check(candidate(metadata={**candidate().metadata, "detail_complete": "false"}), "incomplete_post")


class JozueIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.config = json.loads(Path("config.json").read_text())
        self.config["database"] = str(Path(self.directory.name) / "test.sqlite")
        self.config["sources"]["nehnutelnosti"]["enabled"] = False

    def test_collect_can_select_only_jozue(self):
        with patch("flatwatch.app.JozueClient") as client, patch("flatwatch.app.BazosClient") as bazos, patch("flatwatch.app.fetch_facebook_posts") as facebook:
            client.return_value.fetch.return_value = [candidate()]
            self.assertEqual(len(collect(self.config, include_bazos=False, include_facebook=False)), 1)
            bazos.assert_not_called()
            facebook.assert_not_called()
        self.assertEqual(enabled_sources(self.config, include_bazos=False, include_facebook=False), {"jozue"})

    def test_disabled_source_not_initialized(self):
        self.config["sources"]["jozue"]["enabled"] = False
        with patch("flatwatch.app.JozueClient") as client:
            result = run_check(self.config, include_bazos=False, include_facebook=False, no_telegram=True)
            client.assert_not_called()
        self.assertEqual(result["baseline_sources"], [])

    def test_cli_source_flags(self):
        for command in ("check", "run", "history"):
            args = build_parser().parse_args([command, "--no-bazos", "--no-facebook"])
            self.assertFalse(args.no_jozue)
            args = build_parser().parse_args([command, "--no-jozue"])
            self.assertTrue(args.no_jozue)

    def test_storage_round_trip_and_old_schema_migration(self):
        path = self.config["database"]
        db = sqlite3.connect(path)
        try:
            db.executescript(SCHEMA.replace("    metadata TEXT,\n", ""))
            values = asdict(candidate(source="bazos", source_id="old"))
            values.pop("metadata")
            values["first_seen_at"] = "2026-09-01T12:00:00+00:00"
            values["notified_at"] = "2026-09-01T12:01:00+00:00"
            names = ", ".join(values)
            placeholders = ", ".join("?" for _ in values)
            db.execute(f"INSERT INTO listings ({names}) VALUES ({placeholders})", tuple(values.values()))
            db.commit()
        finally:
            db.close()
        store = ListingStore(path)
        previous = store.connection.execute("SELECT notified_at, metadata FROM listings WHERE source_id='old'").fetchone()
        self.assertEqual(previous["notified_at"], "2026-09-01T12:01:00+00:00")
        self.assertIsNone(previous["metadata"])
        item = candidate()
        store.add(item)
        store.close()
        store = ListingStore(path)
        self.assertEqual(store.pending()[0].metadata, item.metadata)
        store.add(replace(item, metadata={**item.metadata, "status": "prenajaté"}))
        self.assertEqual(store.pending()[0].metadata["status"], "prenajaté")
        store.close()

    def test_baseline_then_one_notification_on_new_ad_no_repeat(self):
        old = candidate()
        fresh = candidate(source_id="202", title="Nový 3-izbový byt na prenájom Železníky",
                          price_eur=780, description="Prenájom nového bytu, terasa a výhľad.")
        notifier = Mock()
        with patch("flatwatch.app.collect", return_value=[old]) as collect_mock, patch("flatwatch.app.notifier_from_config", return_value=notifier):
            first = run_check(self.config, include_bazos=False, include_facebook=False)
            self.assertEqual(first["baseline_sources"], ["jozue"])
            notifier.send_listing.assert_not_called()
            collect_mock.return_value = [old, fresh]
            second = run_check(self.config, include_bazos=False, include_facebook=False)
            third = run_check(self.config, include_bazos=False, include_facebook=False)
            self.assertEqual((second["delivered"], third["delivered"]), (1, 0))
            self.assertEqual(notifier.send_listing.call_count, 1)
        self.assertTrue((Path(self.directory.name) / "latest-jozue-review.json").exists())
        self.assertFalse((Path(self.directory.name) / "latest-facebook-review.json").exists())

    def test_history_includes_jozue_without_writing_database(self):
        with patch("flatwatch.history.JozueClient") as client:
            client.return_value.fetch.return_value = [candidate()]
            items, reasons, fetched = collect_history(self.config, include_bazos=False, include_facebook=False)
            self.assertEqual((len(items), reasons, fetched), (1, {}, 1))
        self.assertFalse(Path(self.config["database"]).exists())

    def test_error_and_recovery_alerts_labeled_jozue(self):
        notifier = Mock()
        with patch("flatwatch.health.notifier_from_config", return_value=notifier):
            health = SourceHealth(self.config)
            health.failure("jozue", JozueParseError("bad markup"))
            health.failure("jozue", JozueParseError("bad markup"))
            health.success("jozue")
            health.success("jozue")
        self.assertEqual(notifier.send_status.call_count, 2)
        self.assertTrue(all("jozue" in call.args[0] for call in notifier.send_status.call_args_list))

    def test_run_routes_error_to_jozue_health(self):
        with patch("flatwatch.app.load_dotenv"), patch("flatwatch.app.load_config", return_value=self.config), patch("flatwatch.app.run_check", side_effect=JozueParseError("broken catalogue")), patch("flatwatch.health.SourceHealth") as health, patch("flatwatch.app.time.sleep", side_effect=KeyboardInterrupt):
            self.assertEqual(main(["run", "--no-bazos", "--no-facebook"]), 130)
            self.assertEqual(health.return_value.failure.call_args.args[0], "jozue")

    def test_telegram_contains_source_and_real_url(self):
        notifier = TelegramNotifier("not-a-real-token", "test-chat")
        with patch.object(notifier, "_request") as send:
            notifier.send_listing(candidate())
        payload = send.call_args.args[1]
        self.assertIn("JOZUE", payload["caption"])
        self.assertEqual(payload["reply_markup"]["inline_keyboard"][0][0]["url"], candidate().url)


if __name__ == "__main__":
    unittest.main()
