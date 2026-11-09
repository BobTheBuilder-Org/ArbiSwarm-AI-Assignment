"""Slow/stuck providers simulated locally: these tests spend no API quota."""
from concurrent.futures import ThreadPoolExecutor
import threading
import time
import unittest
from unittest.mock import patch
from fastapi.testclient import TestClient

from google.genai.errors import ServerError
import httpx

import api
import bounded_work
import config
import lens
import marketplaces
from agents import _llm, runtime
from search_intent import SearchIntent
from tests import test_agent_backend as backend_fixtures
from tests.test_agent_backend import Answer, response
from tests.test_lens import identity, image_bytes, listing


class GenerationDeadlineTests(unittest.TestCase):
    setUp = backend_fixtures.BackendTests.setUp
    tearDown = backend_fixtures.BackendTests.tearDown

    def test_fallback_only_gets_remaining_total_budget(self):
        clock = [1000.0]
        def generate(**kwargs):
            if kwargs['model'] == config.CHEAP_MODEL:
                clock[0] += 20
                raise ServerError(504, {})
            return response()
        with patch.object(_llm.time, 'monotonic', side_effect=lambda: clock[0]), patch('google.genai.Client') as provider, runtime.operation('bounded', 2):
            provider.return_value.models.generate_content.side_effect = generate
            self.assertTrue(_llm.call_json('s', 'u', Answer, role='vision', timeout_ms=60000, total_timeout_ms=45000).ok)
        calls = provider.return_value.models.generate_content.call_args_list
        self.assertEqual([c.kwargs['config']['http_options']['timeout'] for c in calls], [20000, 25000])
        self.assertEqual(provider.call_count, 1)

    def test_expired_budget_never_starts_a_second_wire_attempt(self):
        clock = [1000.0]
        def generate(**kwargs):
            clock[0] += 46
            raise ServerError(504, {})
        with patch.object(_llm.time, 'monotonic', side_effect=lambda: clock[0]), patch('google.genai.Client') as provider, runtime.operation('bounded', 2) as run:
            provider.return_value.models.generate_content.side_effect = generate
            with self.assertRaises(httpx.ReadTimeout):
                _llm.call_json('s', 'u', Answer, timeout_ms=60000, total_timeout_ms=45000)
        self.assertEqual(run.snapshot()['provider_attempts'], 1)
        self.assertEqual(provider.return_value.models.generate_content.call_count, 1)

    def test_operation_deadline_can_shorten_the_call(self):
        with patch('google.genai.Client') as provider, runtime.operation('short', 1, timeout_seconds=3):
            provider.return_value.models.generate_content.return_value = response()
            _llm.call_json('s', 'u', Answer, timeout_ms=60000)
        actual = provider.return_value.models.generate_content.call_args.kwargs['config']['http_options']['timeout']
        self.assertLessEqual(actual, 3000)
        self.assertGreater(actual, 2500)

    def test_bad_primary_is_not_retried_after_a_long_marketplace_wait(self):
        with patch.object(config, 'AI_MODEL_COOLDOWN_SECONDS', 300), patch.object(_llm.time, 'monotonic', return_value=1000), patch('google.genai.Client') as provider:
            generate = provider.return_value.models.generate_content
            generate.side_effect = [ServerError(504, {}), response(), response()]
            _llm.call_json('s', 'u', Answer)
            with patch.object(_llm.time, 'monotonic', return_value=1150):
                _llm.call_json('s', 'u', Answer)
        self.assertEqual([c.kwargs['model'] for c in generate.call_args_list], [config.CHEAP_MODEL, config.FALLBACK_MODEL, config.FALLBACK_MODEL])

    def test_completed_response_after_deadline_is_not_accepted(self):
        clock = [1000.0]
        def generate(**kwargs):
            clock[0] += 50
            return response()
        with patch.object(_llm.time, 'monotonic', side_effect=lambda: clock[0]), patch('google.genai.Client') as provider, runtime.operation('late', 1) as run:
            provider.return_value.models.generate_content.side_effect = generate
            with self.assertRaises(httpx.ReadTimeout):
                _llm.call_json('s', 'u', Answer, timeout_ms=60000, total_timeout_ms=45000)
        self.assertEqual(run.snapshot()['successful_provider_responses'], 1)
        self.assertFalse(run.snapshot()['ai_completed'])


class RetrievalDeadlineTests(unittest.TestCase):
    def tearDown(self):
        with marketplaces._source_lock:
            jobs = list(marketplaces._source_jobs.values())
        for job in jobs:
            job['future'].result(timeout=2)
        with marketplaces._source_lock:
            marketplaces._source_jobs.clear()

    def test_healthy_source_returns_without_waiting_for_stuck_source(self):
        release = threading.Event()
        def slow(*_):
            release.wait(1)
            return []
        try:
            with patch.object(config, 'RETRIEVAL_TIMEOUT_SECONDS', .04), patch.dict(marketplaces.ADAPTERS, {'carousell':lambda *_:[{'id':'ok','title':'Sony headphones'}], 'lazada':slow}):
                at = time.monotonic()
                rows, counts, errors = marketplaces.search_many('Sony headphones', ['lazada','carousell'], 3, intent=SearchIntent(queries=['Sony headphones']))
                self.assertLess(time.monotonic()-at, .5)
            self.assertEqual(counts, {'lazada':0,'carousell':1})
            self.assertEqual(rows[0]['id'], 'ok')
            self.assertTrue(any(e.get('code') == 'SOURCE_TIMEOUT' for e in errors))
        finally:
            release.set()

    def test_alias_timeout_preserves_earlier_rows_and_stops_expansion(self):
        release = threading.Event(); calls = []
        def source(query, *_):
            calls.append(query)
            if query == 'first':return [{'id':'ok','title':'Sony headphones'}]
            release.wait(1)
            return [{'id':'late','title':'Sony headphones'}]
        try:
            with patch.object(config, 'RETRIEVAL_TIMEOUT_SECONDS', .04), patch.dict(marketplaces.ADAPTERS, {'carousell':source}):
                rows, _, errors = marketplaces.search_many('Sony headphones', ['carousell'], 12, intent=SearchIntent(queries=['first','second','third']))
            self.assertEqual([r['id'] for r in rows], ['ok'])
            self.assertEqual(calls, ['first','second'])
            self.assertTrue(errors)
        finally:release.set()

    def test_second_request_does_not_launch_a_duplicate_stalled_adapter(self):
        release=threading.Event();calls=[]
        def source(*_):
            calls.append(1);release.wait(1);return []
        try:
            with patch.object(config, 'RETRIEVAL_TIMEOUT_SECONDS', .04), patch.dict(marketplaces.ADAPTERS, {'carousell':source}):
                intent=SearchIntent(queries=['Sony'])
                marketplaces.search_many('Sony', ['carousell'], 12, intent=intent)
                at=time.monotonic()
                marketplaces.search_many('Sony', ['carousell'], 12, intent=intent)
                self.assertLess(time.monotonic()-at,.1)
            self.assertEqual(len(calls),1)
        finally:release.set()


class OperationAdmissionTests(unittest.TestCase):
    def test_zero_deadline_starts_no_work(self):
        with self.assertRaises(runtime.OperationDeadlineExceeded), patch('bounded_work._pool.submit') as submit:
            bounded_work.run('no-time',lambda:None,0)
        submit.assert_not_called()

    def test_timeout_retry_reuses_existing_work_and_keeps_its_slot(self):
        release=threading.Event();calls=[]
        def work():calls.append(1);release.wait(1);return 'done'
        try:
            with self.assertRaises(runtime.OperationDeadlineExceeded):
                bounded_work.run('stuck-test',work,.03)
            with self.assertRaises(runtime.OperationDeadlineExceeded):
                bounded_work.run('stuck-test',work,.03)
            self.assertEqual(len(calls),1)
        finally:
            release.set()
            bounded_work._jobs['stuck-test'].future.result(timeout=2)

    def test_identical_concurrent_work_is_shared(self):
        release=threading.Event();started=threading.Event();calls=[]
        def work():calls.append(1);started.set();release.wait(1);return 'result'
        with ThreadPoolExecutor(max_workers=2) as callers:
            a=callers.submit(bounded_work.run,'shared-test',work,1)
            self.assertTrue(started.wait(1))
            b=callers.submit(bounded_work.run,'shared-test',work,1)
            time.sleep(.02);release.set()
            self.assertEqual(a.result(),'result');self.assertEqual(b.result(),'result')
        self.assertEqual(len(calls),1)

    def test_no_unbounded_queue_when_all_operation_slots_are_busy(self):
        with patch.object(bounded_work,'_slots',threading.BoundedSemaphore(0)):
            with self.assertRaises(bounded_work.WorkBusy):
                bounded_work.run('full-test',lambda:None,1)

    def test_expired_run_cannot_reserve_a_new_gemini_attempt(self):
        with runtime.operation('expired',2,timeout_seconds=0) as run:
            with self.assertRaises(runtime.OperationDeadlineExceeded):runtime.reserve_attempt()
        self.assertEqual(run.attempts,0)


class ImageWaitTests(unittest.TestCase):
    def test_stuck_image_does_not_hold_the_board_or_trigger_gemini(self):
        release=threading.Event()
        def fetch(*_):release.wait(1);return image_bytes(),'image/png'
        reference=lens.Reference(image_bytes(),identity(),time.monotonic())
        try:
            with patch.object(config,'LENS_IMAGE_TIMEOUT_SECONDS',.03),patch.object(lens,'is_enabled',return_value=True),patch.object(lens,'_fetch_image',side_effect=fetch),patch.object(lens,'call_json') as model:
                at=time.monotonic();result=lens.compare(reference,[listing(777)],'unique stuck-photo test')
                self.assertLess(time.monotonic()-at,.5)
                model.assert_not_called()
            self.assertEqual(result['checked'],0)
            self.assertEqual(result['matches']['carousell:777'].status,'unverified')
        finally:release.set()


class APIWaitTests(unittest.TestCase):
    def test_api_timeout_is_bounded_and_immediate_retry_is_not_duplicated(self):
        release=threading.Event();calls=[]
        def slow(*_):calls.append(1);release.wait(1);return None
        try:
            with patch.object(config,'SEARCH_TIMEOUT_SECONDS',.03),patch.object(api,'_search_operation',side_effect=slow):
                client=TestClient(api.app)
                request={'query':'unique-api-deadline-test','pricing_mode':'auto','source_mode':'demo'}
                first=client.post('/api/search',json=request)
                at=time.monotonic();second=client.post('/api/search',json=request)
                self.assertLess(time.monotonic()-at,.2)
                self.assertEqual(first.status_code,504);self.assertEqual(second.status_code,504)
                self.assertEqual(first.json()['detail']['code'],'OPERATION_TIMEOUT')
                self.assertEqual(len(calls),1)
        finally:
            release.set()
            for job in list(bounded_work._jobs.values()):job.future.result(timeout=2)

    def test_full_operation_admission_returns_clear_busy_status(self):
        with patch.object(bounded_work,'_slots',threading.BoundedSemaphore(0)),patch.object(api,'_search_operation') as work:
            result=TestClient(api.app).post('/api/search',json={'query':'unique-no-slot-test','pricing_mode':'auto','source_mode':'demo'})
        self.assertEqual(result.status_code,503)
        self.assertEqual(result.json()['detail']['code'],'OPERATIONS_BUSY')
        work.assert_not_called()

    def test_expired_lazada_budget_does_not_start_browser_fallback(self):
        token=marketplaces._source_deadline.set(time.monotonic()-1)
        try:
            with patch.object(marketplaces,'_search_lazada_browser') as browser,patch('marketplaces.httpx.get') as network:
                with self.assertRaises(marketplaces.MarketplaceSearchError):marketplaces.search_lazada('Sony',12)
            browser.assert_not_called();network.assert_not_called()
        finally:marketplaces._source_deadline.reset(token)


if __name__=='__main__':unittest.main()
