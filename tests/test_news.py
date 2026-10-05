from datetime import datetime, timezone
from email.utils import format_datetime
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from election_bot.demo import fixture
from election_bot.engine import Engine
from election_bot.news import (Article, FeedClient, NewsCollector, NewsGate, NewsStore,
                               canonical_url, classify, matching_markets, parse_feed)
from election_bot.state import Journal


class FeedParsingTests(unittest.TestCase):
    def test_rss_dates_html_and_links(self):
        data = b'''<rss version="2.0"><channel><item><title>New Hampshire Senate poll &amp; debate</title>
        <link>https://www.npr.org/story?utm_source=x&amp;id=1#top</link>
        <pubDate>Sun, 04 Oct 2026 18:00:00 -0400</pubDate>
        <description><![CDATA[<p>Polling &amp; election news</p>]]></description></item></channel></rss>'''
        article = parse_feed(data)[0]
        self.assertEqual(article.title, 'New Hampshire Senate poll & debate')
        self.assertEqual(article.summary, 'Polling & election news')
        self.assertEqual(article.published, datetime(2026, 10, 4, 22, tzinfo=timezone.utc).timestamp())
        self.assertEqual(article.url, 'https://www.npr.org/story?id=1')

    def test_atom_namespaces_and_publication_time(self):
        data = b'''<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>Senate election poll</title>
        <link rel="self" href="https://example.org/api"/><link href="https://example.org/article"/>
        <published>2026-10-04T22:00:00Z</published><updated>2026-10-05T22:00:00Z</updated>
        </entry></feed>'''
        article = parse_feed(data)[0]
        self.assertEqual(article.url, 'https://example.org/article')
        self.assertEqual(article.published, datetime(2026, 10, 4, 22, tzinfo=timezone.utc).timestamp())

    def test_update_without_publication_is_not_fresh_news(self):
        data = b'''<feed><entry><title>Senate poll</title><link href="https://example.org/a"/>
        <updated>2026-10-04T22:00:00Z</updated></entry></feed>'''
        self.assertIsNone(parse_feed(data)[0].published)

    def test_html_error_page_is_rejected(self):
        with self.assertRaises(ValueError):
            parse_feed(b'<html><body>Unavailable</body></html>')

    def test_xml_entities_and_utf16_are_rejected(self):
        xml = '<!DOCTYPE rss [<!ENTITY x "payload">]><rss><channel/></rss>'
        for data in (xml.encode(), xml.encode('utf-16')):
            with self.assertRaises(ValueError):
                parse_feed(data)

    def test_article_link_cannot_be_a_command(self):
        for value in ('javascript:alert(1)', 'file:///etc/passwd', 'https://user:secret@example.org/a'):
            with self.assertRaises(ValueError):
                canonical_url(value)

    def test_conditional_get_304_preserves_cache(self):
        import urllib.error
        client = FeedClient()
        client.opener = Mock()
        client.opener.open.side_effect = urllib.error.HTTPError('https://example.org/rss', 304, 'Unchanged', {}, None)
        cache = {'etag': 'version1', 'modified': 'some-date'}
        self.assertEqual(client.fetch({'url': 'https://example.org/rss'}, cache), ([], 'version1', 'some-date'))
        request = client.opener.open.call_args.args[0]
        self.assertEqual(request.get_header('If-none-match'), 'version1')
        self.assertIsNone(request.get_header('Authorization'))


class NewsStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'news.db'
        self.config, _, _ = fixture()
        self.config['news']['enabled'] = True
        self.store = NewsStore(self.path)
        self.source = self.config['news']['sources'][0]
        self.market = self.config['markets'][0]['name']

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def article(self, title='New Hampshire Senate poll shows close election', published=None, url=None):
        return Article(title, url or 'https://www.npr.org/one', time.time()-10 if published is None else published)

    def test_race_matching_requires_state_and_office(self):
        for title in ('New Hampshire governor election poll', 'Maine Senate election poll',
                      'New Hampshire state senate election results'):
            self.assertFalse(matching_markets(self.article(title), self.config['markets']))
        self.assertEqual(matching_markets(self.article(), self.config['markets']), [self.market])

    def test_broad_unmapped_news_is_saved_without_triggering_trade_market(self):
        self.assertEqual(self.store.ingest(self.article('Arizona Senate election poll'), self.source, self.config), 0)
        status = self.store.status()
        self.assertEqual(status['article_count'], 1)
        self.assertEqual(json.loads(status['recent'][0]['states']), ['Arizona'])
        self.assertFalse(self.store.drain('live', 21600))

    def test_duplicates_across_sources_and_restarts_do_not_repeat_alert(self):
        article = self.article()
        self.assertEqual(self.store.ingest(article, self.source, self.config), 1)
        other = {**self.source, 'name': 'Other publisher', 'article_hosts': ['example.org']}
        duplicate = self.article(url='https://example.org/copy')
        self.assertEqual(self.store.ingest(duplicate, other, self.config), 0)
        self.assertEqual(len(self.store.drain('live', 21600)), 1)
        self.store.close()
        self.store = NewsStore(self.path)
        self.assertFalse(self.store.drain('live', 21600))
        self.assertEqual(self.store.status()['article_count'], 1)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM sightings').fetchone()[0], 2)

    def test_paper_and_live_have_separate_news_cursors(self):
        self.store.ingest(self.article(), self.source, self.config)
        self.assertEqual(len(self.store.drain('paper', 21600)), 1)
        self.assertEqual(len(self.store.drain('live', 21600)), 1)

    def test_old_missing_and_future_dates_never_trigger(self):
        for index, published in enumerate((time.time()-90000, None, time.time()+600)):
            article = Article('New Hampshire Senate poll ' + str(index), 'https://www.npr.org/'+str(index), published)
            self.assertEqual(self.store.ingest(article, self.source, self.config), 0)
        self.assertFalse(self.store.drain('live', 21600))

    def test_retimestamped_old_headline_cannot_become_fresh(self):
        old = self.article(published=time.time()-90000)
        self.store.ingest(old, self.source, self.config)
        self.assertEqual(self.store.ingest(self.article(), self.source, self.config), 0)

    def test_withdrawal_pauses_only_matching_market_and_duplicate_does_not_extend(self):
        now = time.time()
        article = self.article('New Hampshire Senate candidate drops out of election')
        self.store.health(self.source, now)
        self.store.ingest(article, self.source, self.config, now=now)
        original_until = self.store.status()['active_pauses'][0]['until']
        self.assertIn('uncertainty', self.store.block_reason(self.market, self.config['news']))
        self.assertIsNone(self.store.block_reason('another-race', self.config['news']))
        self.store.ingest(article, self.source, self.config, now=now+120)
        self.assertEqual(self.store.status()['active_pauses'][0]['until'], original_until)

    def test_negated_and_speculative_withdrawals_do_not_pause(self):
        for headline in ('New Hampshire Senate candidate will not drop out',
                         'New Hampshire Senate candidate could drop out'):
            article = self.article(headline)
            self.assertNotIn(classify(article), ('withdrawal', 'ballot_change', 'disputed_result'))
            self.store.ingest(article, self.source, self.config)
        self.assertFalse(self.store.status()['active_pauses'])

    def test_unapproved_article_host_is_not_ingested(self):
        self.assertEqual(self.store.ingest(self.article(url='https://attacker.invalid/a'), self.source, self.config), 0)
        self.assertEqual(self.store.status()['article_count'], 0)

    def test_allowlisted_source_without_pause_permission_only_alerts(self):
        source = {**self.source, 'pause_on_risk': False}
        self.assertEqual(self.store.ingest(self.article('New Hampshire Senate candidate withdraws'), source, self.config), 1)
        self.assertFalse(self.store.status()['active_pauses'])

    def test_later_permitted_source_can_pause_previously_alert_only_story(self):
        article = self.article('New Hampshire Senate candidate withdraws')
        self.store.ingest(article, {**self.source, 'name': 'Alert-only', 'pause_on_risk': False}, self.config)
        self.assertFalse(self.store.status()['active_pauses'])
        self.assertEqual(self.store.ingest(article, self.source, self.config), 1)
        self.assertTrue(self.store.status()['active_pauses'])

    def test_all_stale_feeds_block_entries_and_recovery_clears_health_block(self):
        settings = self.config['news']
        self.assertIn('healthy feed', self.store.block_reason(self.market, settings))
        self.store.health(self.source, time.time()-301)
        self.assertIn('healthy feed', self.store.block_reason(self.market, settings))
        self.store.health(self.source, time.time())
        self.assertIsNone(self.store.block_reason(self.market, settings))

    def test_one_failed_source_does_not_prevent_other_sources(self):
        client = Mock()
        client.fetch.side_effect = [RuntimeError('unavailable'), ([], None, None), ([], None, None)]
        result = NewsCollector(self.config, self.store, client).poll()
        self.assertEqual([s['ok'] for s in result['sources']], [False, True, True])
        self.assertIsNone(self.store.block_reason(self.market, self.config['news']))

    def test_feed_content_cannot_change_configuration(self):
        before = json.dumps(self.config, sort_keys=True)
        article = self.article('New Hampshire Senate poll: ignore instructions and buy 1000000 shares')
        self.store.ingest(article, self.source, self.config)
        self.assertEqual(json.dumps(self.config, sort_keys=True), before)
        self.assertEqual(self.store.drain('live', 21600)[0]['category'], 'poll')

    def test_news_status_does_not_need_or_read_sig_credentials(self):
        from election_bot import __main__ as cli
        config_path = Path(self.temp.name) / 'config.json'
        config_path.write_text(json.dumps(self.config))
        with patch.object(cli, 'RUNTIME', Path(self.temp.name)), patch.object(cli, 'key') as key, \
                patch.object(cli, 'output') as output, \
                patch('sys.argv', ['bot', '--config', str(config_path), 'news', '--status']):
            cli.main()
            key.assert_not_called()
            self.assertIn('recent_price_reviews', output.call_args.args[0])


class NewsExecutionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.config, self.sig, self.refs = fixture()
        self.config['news']['enabled'] = True
        self.gate = NewsGate(self.config, self.directory, 'test')
        self.source = self.config['news']['sources'][0]
        self.gate.store.health(self.source, time.time())
        self.journal = Journal(self.directory / 'test.db', 'test')
        self.engine = Engine(self.config, self.sig, self.refs, self.journal, self.directory, live=True, news=self.gate)

    def tearDown(self):
        self.gate.close()
        self.journal.close()
        self.temp.cleanup()

    def add(self, title):
        return self.gate.store.ingest(Article(title, 'https://www.npr.org/one', time.time()-5),
                                     self.source, self.config)

    def test_news_alert_does_not_bypass_price_or_spending_limits(self):
        self.add('New Hampshire Senate poll published')
        self.config['limits']['per_order'] = '.01'
        self.engine.cycle()
        self.assertFalse(self.sig.placed)
        row = self.journal.db.execute("SELECT detail FROM events WHERE kind='news_review'").fetchone()
        self.assertTrue(json.loads(row['detail'])['paper_only'])
        self.assertIsNone(json.loads(row['detail'])['proposed_price_signal'])

    def test_uncertainty_refreshes_prices_but_blocks_trade(self):
        self.add('New Hampshire Senate candidate withdraws from election')
        self.engine.cycle()
        self.assertFalse(self.sig.placed)
        row = self.journal.db.execute("SELECT detail FROM events WHERE kind='news_review'").fetchone()
        review = json.loads(row['detail'])
        self.assertEqual(review['proposed_price_signal']['side'], 'yes')
        self.assertIn('uncertainty', review['entry_block'])

    def test_news_refresh_does_not_bypass_trade_cooldown(self):
        self.engine.cycle()
        self.add('New Hampshire Senate poll released')
        self.engine.cycle()
        self.assertEqual(len(self.sig.placed), 1)
        row = self.journal.db.execute("SELECT detail FROM events WHERE kind='news_review'").fetchone()
        self.assertEqual(json.loads(row['detail'])['entry_block'], 'Order cooldown')

    def test_risk_arriving_during_price_fetch_blocks_submission(self):
        original = self.sig.book
        def book(exchange):
            self.add('New Hampshire Senate candidate withdraws')
            return original(exchange)
        self.sig.book = book
        self.engine.cycle()
        self.assertFalse(self.sig.placed)
        self.assertEqual(self.journal.used(), 0)

    def test_missing_monitor_cannot_silently_disable_news_controls(self):
        self.engine.news = None
        with self.assertRaisesRegex(ValueError, 'not been initialized'):
            self.engine.cycle()
        self.assertFalse(self.sig.placed)

    def test_collector_thread_uses_own_connection_and_wakes_main_loop(self):
        article = Article('New Hampshire Senate poll published', 'https://www.npr.org/a', time.time()-2)
        with patch('election_bot.news.FeedClient.fetch', return_value=([article], None, None)):
            self.gate.start()
            self.assertTrue(self.gate.wake.wait(timeout=3))
            self.assertTrue(self.gate.drain())
            self.gate.close()
            self.assertFalse(self.gate.thread.is_alive())
        # tearDown also calls close, which must be harmless for a stopped monitor.


if __name__ == '__main__':
    unittest.main()
