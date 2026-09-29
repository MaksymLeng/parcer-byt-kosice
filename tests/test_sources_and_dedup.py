import json
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from flatwatch.app import enabled_sources, run_check
from flatwatch.bazos import BazosClient, BazosParseError
from flatwatch.core import Listing, matches_filters
from flatwatch.nehnutelnosti import NehnutelnostiClient, NehnutelnostiParseError, parse_nehnutelnosti_html
from flatwatch.storage import ListingStore


NOW = datetime.now(timezone.utc).isoformat()
DESCRIPTION = ('Ponúkame na prenájom 3 izbový byt, Košice II - Terasa, OV, 56 m2, '
               'na 3.posch, 1x loggia, bez výťahu. V byte sú plastové okná, '
               'murované jadro, plávajúce podlahy. Byt sa prenajíma zariadený. '
               'Výška nájmu je 700 EUR/mesačne vrátane energií.')


def listing(source='facebook', source_id='10', title='3 izbový byt Košice',
            description=DESCRIPTION, price=700, first_seen_at=NOW):
    return Listing(source, source_id, f'https://example.test/{source_id}', title,
                   description, price, 'Košice', published_at=NOW,
                   first_seen_at=first_seen_at)


class DedupTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = ListingStore(Path(self.directory.name) / 'listings.sqlite')

    def tearDown(self):
        self.store.close()
        self.directory.cleanup()

    def test_same_first_seen_and_smaller_id_does_not_match_itself(self):
        self.store.add(listing(source_id='20'))
        self.store.mark_notified(listing(source_id='20'))
        self.store.add(listing(source_id='10'))
        self.assertTrue(self.store.is_content_duplicate(listing(source_id='10')))

    def test_cross_site_same_full_description_different_headline(self):
        first = listing('bazos', '20', title='Prenájom plne zariadeného trojizbového bytu na Uherovej ulici v Košiciach')
        second = listing('nehnutelnosti', 'new', title='3 izbový byt na prenájom, Košice II - Západ, ulica Uherova, 56 m2')
        self.store.add(first)
        self.store.mark_notified(first)
        self.assertTrue(self.store.is_content_duplicate(second))

    def test_cross_site_minor_formatting_difference(self):
        first = listing('bazos', '20', title='Prenájom plne zariadeného trojizbového bytu na Uherovej ulici v Košiciach')
        second = listing('nehnutelnosti', 'new', title='3 izbový byt na prenájom, Košice II - Západ, ulica Uherova, 56 m2',
                         description=DESCRIPTION.replace('3.posch', '3. posch'))
        self.store.add(first)
        self.store.mark_notified(first)
        self.assertTrue(self.store.is_content_duplicate(second))

    def test_different_apartment_same_price_and_rooms_not_duplicate(self):
        first = listing('bazos', '20')
        second = listing('nehnutelnosti', 'new', description=(
            'Prenajmem 3 izbový byt na Hlavnej ulici, Košice, 80 m2. '
            'Ponúkam vybavenú kuchyňu, nábytok a dostupné parkovanie pri dome. '
            'Cena mesačného nájmu je 700 € vrátane energií.'))
        self.store.add(first)
        self.store.mark_notified(first)
        self.assertFalse(self.store.is_content_duplicate(second))

    def test_old_bazos_preview_matches_full_cross_site_ad(self):
        body = ('MAXEN REALITY CENTRUM ponúka na prenájom 2 izbový byt na Južnej triede, '
                'ktorý sa nachádza na 2.poschodí. Byt je veľmi pekne zariadený, '
                'priestranný /52m2/ a kompletne zariadený. Byt: priestranný a svetlý, '
                'pekne a kvalitne zariadený, umývačka riadu, práčka, internet. '
                'Hlavné výhody lokality a bytového domu: Dostupnosť do centra mesta.')
        old = listing('bazos', 'old', 'Prenájom pekný a priestranný 2 izbový byt, Košice - JUH.',
                      body[:290].replace('52m2', '5 2 m 2'), 750)
        new = listing('nehnutelnosti', 'new', old.title,
                      body + ' Orientácia domu je na východ a západ; zariadenie je kvalitné.', 750)
        self.store.add(old)
        self.store.mark_notified(old)
        self.assertTrue(self.store.is_content_duplicate(new))

    def test_same_title_and_price_without_matching_description_is_not_duplicate(self):
        old = listing('bazos', 'old', '2-izbový byt na prenájom v Košiciach - centrum',
                      DESCRIPTION, 750)
        new = listing('nehnutelnosti', 'new', old.title,
                      'Celkom iný byt na inej ulici. ' * 12, 750)
        self.store.add(old)
        self.store.mark_notified(old)
        self.assertFalse(self.store.is_content_duplicate(new))

    def test_notified_items_never_become_pending_again(self):
        old = listing('facebook', '10')
        self.store.add(old)
        self.store.mark_notified(old)
        self.store.add(listing('facebook', '10', title='updated title'))
        self.assertEqual(self.store.pending({'facebook'}), [])

    def test_parallel_sources_claim_one_notification_atomically(self):
        gate = threading.Barrier(2)
        results = []
        errors = []

        def claim(item):
            try:
                store = ListingStore(self.store.path)
                gate.wait(timeout=5)
                results.append(store.add_if_unique(item))
                store.close()
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=claim, args=(item,)) for item in (
            listing('bazos', '20'), listing('nehnutelnosti', '21'))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=8)
        self.assertFalse(errors)
        self.assertEqual(sorted(results), [(False, True), (True, False)])
        self.assertEqual(len(self.store.pending()), 1)


URL = 'https://www.nehnutelnosti.sk/vysledky/byty/kosice/prenajom'
DETAIL = 'https://www.nehnutelnosti.sk/detail/JuLiDok_QlG/3-izbovy-byt'


def site_html(entries, next_page=None):
    items = []
    for index, entry in enumerate(entries, 1):
        items.append({'position': index, 'item': {
            'url': DETAIL.replace('JuLiDok_QlG', entry.get('id', 'JuLiDok_QlG')),
            'name': entry.get('title', '3 izbový byt na prenájom'),
            'description': entry.get('description', DESCRIPTION),
            'category': '3 izbový byt', 'validFrom': entry.get('date', NOW),
            'priceSpecification': {'price': entry.get('price', 700)},
            'image': ['https://img.example.test/a.jpg'],
            'availability': 'https://schema.org/InStock',
            'numberOfRooms': 3,
        }})
    graph = {'@graph': [{'@type': 'SearchResultsPage', 'mainEntity': {
        'numberOfItems': len(items), 'itemListElement': items}}]}
    next_link = f'<a href="{URL}?page={next_page}">Ďalšie</a>' if next_page else ''
    return ('<html><script type="application/ld+json">' + json.dumps(graph) +
            '</script><h1>Byty na prenájom, Košice</h1>' + next_link + '</html>')


class NehnutelnostiTests(unittest.TestCase):
    def test_parse_publication_date_and_full_description(self):
        items, next_page = parse_nehnutelnosti_html(site_html([{}]), URL)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].published_at, NOW)
        self.assertEqual(items[0].description, DESCRIPTION)
        self.assertEqual(items[0].price_eur, 700)
        self.assertIsNone(next_page)

    def test_rejects_missing_date_instead_of_treating_as_new(self):
        html = site_html([{}]).replace('"validFrom": "' + NOW + '"', '"validFrom": null')
        with self.assertRaises(NehnutelnostiParseError):
            parse_nehnutelnosti_html(html, URL)

    def test_old_promoted_ad_does_not_pass_age_filter(self):
        old = (datetime.now(timezone.utc) - timedelta(days=4)).isoformat()
        item = parse_nehnutelnosti_html(site_html([{'date': old}]), URL)[0][0]
        self.assertEqual(matches_filters(item, {'city': 'Košice', 'rooms': [2, 3],
            'price_min': 500, 'price_max': 800})[:2], (False, 'stale_listing'))

    def test_missing_publication_time_is_not_sent(self):
        item = listing('nehnutelnosti', 'new')
        item = Listing(item.source, item.source_id, item.url, item.title,
                       item.description, item.price_eur, item.location,
                       metadata={'availability': 'https://schema.org/InStock',
                                 'property_type': '3 izbový byt'})
        self.assertEqual(matches_filters(item, {'city': 'Košice', 'rooms': [2, 3],
            'price_min': 500, 'price_max': 800})[:2], (False, 'published_time_missing'))

    def test_bazos_kosice_okolie_is_not_in_city(self):
        item = listing('bazos', 'new')
        item = Listing(item.source, item.source_id, item.url, item.title,
                       item.description, item.price_eur, 'Košice-okolie 044 23',
                       published_at=NOW)
        self.assertEqual(matches_filters(item, {'city': 'Košice', 'rooms': [2, 3],
            'price_min': 500, 'price_max': 800})[:2], (False, 'outside_city'))

    def test_all_pages_and_repeated_premium_card_deduped(self):
        client = NehnutelnostiClient(max_pages=3, request_delay_seconds=0)
        client.session.get = Mock(side_effect=[
            Mock(status_code=200, text=site_html([{}], next_page=2), url=URL,
                 raise_for_status=Mock()),
            Mock(status_code=200, text=site_html([{}, {'id': 'another'}]),
                 url=URL + '?page=2', raise_for_status=Mock()),
        ])
        items = client.fetch(URL)
        self.assertEqual([item.source_id for item in items], ['JuLiDok_QlG', 'another'])
        self.assertEqual(client.session.get.call_count, 2)

    def test_page_limit_is_error_not_silent_truncation(self):
        client = NehnutelnostiClient(max_pages=1, request_delay_seconds=0)
        client.session.get = Mock(return_value=Mock(text=site_html([{}], next_page=2),
                                                     url=URL, raise_for_status=Mock()))
        with self.assertRaises(NehnutelnostiParseError):
            client.fetch(URL)


class BazosPagingTests(unittest.TestCase):
    def test_fetches_second_page_and_deduplicates_cards(self):
        client = BazosClient()
        def page(ids, next_offset=None):
            cards = ''.join('<div class="inzeraty inzeratyflex"><h2 class=nadpis>'
                f'<a href="/inzerat/{i}/flat.php">3 izbový byt Košice</a></h2>'
                '<div class="inzeratycena"><b><span>700 €</span></b></div></div>' for i in ids)
            nxt = (f'<a href="/prenajmu/byt/{next_offset}/?hledat=3+izbovy">Ďalšia</a>'
                   if next_offset else '')
            return cards + nxt
        url = 'https://reality.bazos.sk/prenajmu/byt/?hledat=3+izbovy'
        client.session.get = Mock(side_effect=[
            Mock(text=page(['10'], 20), url=url, raise_for_status=Mock()),
            Mock(text=page(['10', '11']), url=url.replace('/byt/', '/byt/20/'),
                 raise_for_status=Mock()),
        ])
        self.assertEqual([i.source_id for i in client.fetch(url)], ['10', '11'])
        self.assertEqual(client.session.get.call_count, 2)

    def test_pagination_limit_fails_loudly(self):
        client = BazosClient(max_pages=1)
        url = 'https://reality.bazos.sk/prenajmu/byt/?hledat=izba'
        client.session.get = Mock(return_value=Mock(
            text='<div class="inzeraty inzeratyflex"><h2 class=nadpis>'
                 '<a href="/inzerat/123/flat.php">3 izbový byt</a></h2></div>'
                 '<a href="/prenajmu/byt/20/?hledat=izba">Ďalšia</a>',
            url=url, raise_for_status=Mock()))
        with self.assertRaises(BazosParseError):
            client.fetch(url)


class AppIntegrationTests(unittest.TestCase):
    def test_playwright_package_matches_browser_image(self):
        import re
        dockerfile = Path('Dockerfile').read_text()
        requirements = Path('requirements-facebook.txt').read_text()
        image_version = re.search(r'playwright/python:v([\d.]+)-noble', dockerfile)
        package_version = re.search(r'^playwright==([\d.]+)$', requirements, re.M)
        self.assertIsNotNone(image_version)
        self.assertIsNotNone(package_version)
        if image_version and package_version:
            self.assertEqual(image_version[1], package_version[1])

    def test_new_source_baselines_without_telegram(self):
        with tempfile.TemporaryDirectory() as directory:
            config = {'database': str(Path(directory) / 'db.sqlite'),
                      'sources': {'bazos': {'enabled': False}, 'facebook': {'enabled': False},
                                  'jozue': {'enabled': False},
                                  'nehnutelnosti': {'enabled': True, 'url': URL}},
                      'filters': {'city': 'Košice', 'rooms': [2, 3],
                                  'price_min': 500, 'price_max': 800},
                      'telegram': {'bot_token_env': 'NONE', 'chat_id_env': 'NONE'}}
            self.assertEqual(enabled_sources(config), {'nehnutelnosti'})
            candidate = parse_nehnutelnosti_html(site_html([{}]), URL)[0][0]
            notifier = Mock()
            with patch('flatwatch.app.collect', return_value=[candidate]), \
                 patch('flatwatch.app.notifier_from_config', return_value=notifier):
                result = run_check(config)
            self.assertEqual(result['baseline_sources'], ['nehnutelnosti'])
            self.assertEqual(result['delivered'], 0)
            notifier.send_listing.assert_not_called()
            store = ListingStore(config['database'])
            try:
                self.assertEqual(store.pending({'nehnutelnosti'}), [])
            finally:
                store.close()


if __name__ == '__main__':
    unittest.main()
