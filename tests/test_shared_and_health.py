import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from flatwatch.core import Listing, find_price, find_rooms, find_preferred_area, is_shared_offer, matches_filters
from flatwatch.facebook import parse_facebook_published
from flatwatch.health import SourceHealth


class HousingIntentTests(unittest.TestCase):
    def check(self, text, price=300):
        config = json.loads(Path('config.json').read_text())
        item = Listing('facebook', 'test', 'https://example.test/test', text, '', price,
                       'Košice', published_at=datetime.now(timezone.utc).isoformat())
        return matches_filters(item, config['filters'])[:2]

    def test_room_offer_without_total_room_count_and_below_500(self):
        for text in ('Ponúkam samostatnú izbu za 300 €', 'Room available for rent 300 €',
                     'Здається окрема кімната за 300 €', 'Сдам комнату в квартире 300 €',
                     'Voľná izba v Košiciach. Cena 300 €'):
            with self.subTest(text=text):
                self.assertEqual(self.check(text), (True, 'matched'))

    def test_existing_flat_seeking_occupant(self):
        for text in ('Hľadáme spolubývajúceho do 3-izbového bytu, nájom 300 €',
                     'Ищем соседа в квартиру, свободная комната, 300 €',
                     'Looking for a roommate in our flat. Room available 300 €'):
            with self.subTest(text=text):
                self.assertTrue(is_shared_offer(text))
                self.assertEqual(self.check(text), (True, 'matched'))

    def test_people_searching_for_housing_still_rejected(self):
        for text in ('Hľadám nepriechodnú izbu do 300 €', 'Ищу подселение в квартиру до 300 €',
                     'Шукаю кімнату, бюджет 300 €', 'Looking for a room in Košice',
                     'Looking for roommates. We can look for an apartment together!'):
            with self.subTest(text=text):
                self.assertEqual(self.check(text), (False, 'wanted_post'))

    def test_real_landlord_tenant_search_is_offer(self):
        text = 'Do 2-izbového bytu na Jantárovej hľadám nájomcov. Voľný na nasťahovanie hneď.'
        self.assertEqual(self.check(text, 700), (True, 'matched'))
        self.assertEqual(self.check(text, 800), (True, 'matched'))
        self.assertEqual(self.check(text, 810), (False, 'price_outside_range'))

    def test_whole_studios_still_excluded(self):
        self.assertEqual(self.check('Prenajmem 1-izbový byt 600 €', 600), (False, 'wrong_rooms'))

    def test_price_and_grammar(self):
        self.assertEqual(find_price('Depozit 1 200 €, nájom 600 €'), 600)
        self.assertEqual(find_price('Cena 1 200 €'), 1200)
        self.assertEqual(find_rooms('v 3-izbovom byte'), 3)
        self.assertEqual(find_price('Nájom 700,- € a depozit 1.500,- €'), 700)
        self.assertEqual(find_price('Cena izby 370,- €'), 370)

    def test_truncated_facebook_body_is_not_safe_to_send(self):
        item = Listing('facebook', 'test', 'https://example.test/test', 'Room available',
                       'Room available 485 €. Conditions: See more', 485, 'Košice',
                       published_at=datetime.now(timezone.utc).isoformat())
        filters = json.loads(Path('config.json').read_text())['filters']
        self.assertEqual(matches_filters(item, filters)[:2], (False, 'incomplete_post'))

    def test_known_utilities_exactly_at_budget(self):
        self.assertEqual(self.check('Prenajmem 2-izbový byt, 700 € + energie 100 €', 700), (True, 'matched'))

    def test_female_targeted_room_offers_are_excluded(self):
        for text in ('Room available 500 €. I would like a girl also studying medicine.',
                     'Room available 485 €. Only girl tenant (2 others are girl).',
                     'Ponúkam izbu len pre dievča 300 €',
                     'Ponúkam izbu pre 4 študentky 300 €',
                     'Сдам комнату только для девушек 300 €',
                     'Здається кімната для дівчини 300 €'):
            with self.subTest(text=text):
                self.assertEqual(self.check(text), (False, 'female_only'))
        self.assertEqual(self.check('Room available 300 €. Two other roommates are girls.'), (True, 'matched'))
        self.assertEqual(self.check('Room available, male or female tenant welcome'), (True, 'matched'))

    def test_real_ad_words_do_not_reverse_offer_direction(self):
        self.assertEqual(self.check('Looking for a comfortable home? We offer for rent a 2-room apartment. Rent €780', 780), (True, 'matched'))
        self.assertEqual(self.check('For rent 2-room apartment 790 €, private reserved parking spot included', 790), (True, 'matched'))
        self.assertTrue(is_shared_offer('Prenájom samostatnej spálne v 3-izbovom byte'))
        self.assertEqual(find_price('Rent €780 / month, deposit €1,000'), 780)

    def test_trip_places_are_not_shared_housing(self):
        self.assertFalse(is_shared_offer('ЗАЛИШИЛОСЯ ЛИШЕ 1 ВІЛЬНЕ МІСЦЕ! Подорож літаком 250 €'))

    def test_mandatory_parking_is_in_budget(self):
        self.assertEqual(self.check('Do 2-izbového bytu hľadám nájomcov. Cena 770€ + 50€ parkovacie miesto', 770), (False, 'price_outside_range'))

    def test_shopping_centre_does_not_mean_city_centre(self):
        self.assertIsNone(find_preferred_area('Dargovských hrdinov, Benadova, blízko obchodné centrum'))
        self.assertEqual(find_preferred_area('Byt v centre Košíc, Vrátna'), 'Centrum')

    def test_absolute_date_is_publication_date(self):
        self.assertEqual(parse_facebook_published('Пятница, 4 сентябрь 2026 г. в 18:21'), '2026-09-04T16:21:00+00:00')
        self.assertEqual(parse_facebook_published('July 12, 2026 at 10:30 AM'), '2026-07-12T08:30:00+00:00')


class HealthTests(unittest.TestCase):
    def test_auth_alert_is_persistent_and_recovery_sent_once(self):
        with tempfile.TemporaryDirectory() as directory:
            config = {'database': str(Path(directory) / 'test.sqlite')}
            notifier = Mock()
            with patch('flatwatch.health.notifier_from_config', return_value=notifier):
                SourceHealth(config).failure('facebook', RuntimeError('Facebook session is not logged in'))
                SourceHealth(config).failure('facebook', RuntimeError('Facebook session is not logged in'))
                self.assertEqual(notifier.send_status.call_count, 1)
                SourceHealth(config).success('facebook')
                SourceHealth(config).success('facebook')
                self.assertEqual(notifier.send_status.call_count, 2)
                SourceHealth(config).failure('facebook', RuntimeError('Facebook session is not logged in'))
                self.assertEqual(notifier.send_status.call_count, 3)


if __name__ == '__main__':
    unittest.main()
