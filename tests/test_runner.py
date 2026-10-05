import unittest
import socket
import ssl
import urllib.error
from unittest.mock import Mock, patch

from election_bot.clients import APIError, HTTP
from election_bot.runner import connect, run_loop, wait


class RunnerTests(unittest.TestCase):
    def engine(self, outcomes):
        engine = Mock()
        engine.stopped.return_value = False
        engine.journal.pending.return_value = []
        engine.config = {'poll_seconds': 20}
        engine.cycle.side_effect = outcomes
        return engine

    def test_backoff_caps_and_resets_after_success(self):
        error = APIError('503', status=503, method='GET')
        engine = self.engine([error]*6 + [True, error, False])
        with patch('election_bot.runner.wait', return_value=True) as pause:
            run_loop(engine)
        self.assertEqual([c.args[1] for c in pause.call_args_list], [5, 10, 20, 40, 60, 60, 20, 5])
        restored = [c for c in engine.report.call_args_list if c.args[0] == 'connection_restored']
        self.assertEqual(len(restored), 2)

    def test_rate_limit_delay_cannot_be_shortened_by_news(self):
        engine = self.engine([APIError('429', status=429, method='GET', retry_after=120), False])
        news = Mock()
        news.wake.is_set.return_value = True
        with patch('election_bot.runner.wait', return_value=True) as pause:
            run_loop(engine, news=news)
        pause.assert_called_once_with(engine, 120)

    def test_writes_auth_and_unknown_failures_still_halt(self):
        errors = [APIError('503', status=503, method=method) for method in ('POST', 'DELETE', None)]
        errors += [APIError(str(code), status=code, method='GET') for code in (401, 403, 404)]
        errors += [RuntimeError('Unknown order status'), KeyboardInterrupt()]
        for error in errors:
            with self.subTest(error=error), patch('election_bot.runner.wait') as pause:
                engine = self.engine([error])
                with self.assertRaises(type(error)):
                    run_loop(engine)
                pause.assert_not_called()
                self.assertEqual(engine.cycle.call_count, 1)

    def test_once_propagates_outage(self):
        engine = self.engine([APIError('503', status=503, method='GET')])
        with self.assertRaises(APIError), patch('election_bot.runner.wait') as pause:
            run_loop(engine, once=True)
        pause.assert_not_called()

    def test_stop_during_recovery_prevents_another_cycle(self):
        engine = self.engine([APIError('timeout', method='GET'), True])
        with patch('election_bot.runner.wait', return_value=False):
            run_loop(engine)
        self.assertEqual(engine.cycle.call_count, 1)

    def test_wait_checks_stop_without_sleeping_entire_delay(self):
        engine = self.engine([])
        engine.stopped.side_effect = [False, True]
        with patch('election_bot.runner.time.sleep') as sleep:
            self.assertFalse(wait(engine, 120))
        sleep.assert_called_once()
        self.assertLessEqual(sleep.call_args.args[0], .25)


class ErrorProvenanceTests(unittest.TestCase):
    def test_network_diagnostics_and_certificate_failure(self):
        http = HTTP('https://sig.thesuper.market/api/v1')
        http.opener = Mock()
        http.opener.open.side_effect = urllib.error.URLError(socket.gaierror('private detail'))
        with self.assertRaisesRegex(APIError, 'DNS lookup failed'):
            http.request('/tournaments/test')
        http.opener.open.side_effect = urllib.error.URLError(ssl.SSLCertVerificationError('private detail'))
        with self.assertRaisesRegex(RuntimeError, 'TLS certificate verification failed') as caught:
            http.request('/tournaments/test')
        self.assertNotIsInstance(caught.exception, APIError)
        self.assertNotIn('private detail', str(caught.exception))

    def test_http_and_network_errors_preserve_request_method(self):
        for method in ('GET', 'POST', 'DELETE'):
            for failure in (urllib.error.HTTPError('https://example.invalid', 503, 'Unavailable', {}, None),
                            urllib.error.URLError('offline')):
                with self.subTest(method=method, failure=type(failure).__name__):
                    http = HTTP('https://sig.thesuper.market/api/v1')
                    http.opener = Mock()
                    http.opener.open.side_effect = failure
                    with self.assertRaises(APIError) as caught:
                        http.request('/orders/1', method=method)
                    self.assertEqual(caught.exception.method, method)


class StartupTests(unittest.TestCase):
    def test_network_then_http_failure_reconnects(self):
        client, reporter = object(), Mock()
        factory = Mock(side_effect=[APIError('URLError', method='GET'),
                                   APIError('503', status=503, method='GET'), client])
        with patch('election_bot.runner.wait_until', return_value=True) as pause:
            self.assertIs(connect(factory, lambda: False, reporter), client)
        self.assertEqual([c.args[1] for c in pause.call_args_list], [5, 10])
        self.assertEqual(reporter.call_args.args[0]['event'], 'startup_connection_restored')

    def test_startup_honors_retry_after_and_stop(self):
        factory = Mock(side_effect=APIError('429', status=429, retry_after=120, method='GET'))
        with patch('election_bot.runner.wait_until', return_value=False) as pause:
            self.assertIsNone(connect(factory, lambda: False, Mock()))
        self.assertEqual(pause.call_args.args[1], 120)
        factory.assert_called_once()

    def test_existing_stop_flag_prevents_network_access(self):
        factory = Mock()
        self.assertIsNone(connect(factory, lambda: True, Mock()))
        factory.assert_not_called()

    def test_nontransient_errors_and_interrupt_propagate(self):
        for error in (APIError('401', status=401, method='GET'),
                      APIError('write failed', status=503, method='POST'),
                      ValueError('Invalid tournament'), KeyboardInterrupt()):
            with self.subTest(error=error), patch('election_bot.runner.wait_until') as pause:
                with self.assertRaises(type(error)):
                    connect(Mock(side_effect=error), lambda: False, Mock())
                pause.assert_not_called()
