"""Regression coverage for the rejected analyst request and hidden fallbacks."""
import json
import subprocess
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfoNotFoundError

import httpx
from google import genai
from google.genai.errors import ClientError, ServerError
from pydantic import ValidationError

import api
import config
import copilot
import lens
import search_intent
from agents import _llm, runtime, batch_analyst as analyst
from agents.json_schema import provider_schema
from tests import test_agent_backend as fixtures
from tests.test_core import listing
from scripts import check_gemini

_real_send = httpx.Client.send
_real_client = genai.Client


class ReliabilityTests(unittest.TestCase):
    setUp = fixtures.BackendTests.setUp
    tearDown = fixtures.BackendTests.tearDown

    def test_timeout_uses_one_fallback_model_on_same_credential(self):
        for error in (ServerError(504, {}), httpx.ReadTimeout('private request')):
            with self.subTest(error=type(error).__name__):
                _llm._model_cooldowns.clear()
                with patch('google.genai.Client') as provider, runtime.operation('timeout_recovery', 2) as run:
                    generate = provider.return_value.models.generate_content
                    generate.side_effect = [error, fixtures.response()]
                    self.assertTrue(_llm.call_json('s', 'u', fixtures.Answer, role='vision').ok)
                    runtime.record('vision', 'ai_completed')
                self.assertEqual(provider.call_count, 1)
                self.assertEqual([call.kwargs['model'] for call in generate.call_args_list],
                                 [config.CHEAP_MODEL, config.FALLBACK_MODEL])
                report = run.snapshot()
                self.assertEqual(report['provider_attempts'], 2)
                self.assertEqual(report['successful_provider_responses'], 1)
                self.assertEqual(report['token_usage']['total_token_count'], 25)
                self.assertFalse(report['usage_complete'])
                self.assertEqual(report['stages']['vision']['state'], 'completed')
                self.assertTrue(any(event['status'] == 'model_fallback' for event in report['events']))
                self.assertNotIn('private request', json.dumps(report))

    def test_exhausted_operation_preserves_timeout_without_another_attempt(self):
        with patch('google.genai.Client') as provider, runtime.operation('one_attempt', 1) as run:
            provider.return_value.models.generate_content.side_effect = ServerError(504, {})
            with self.assertRaises(ServerError) as raised:
                _llm.call_json('s', 'u', fixtures.Answer)
        self.assertEqual(raised.exception.code, 504)
        self.assertEqual(run.snapshot()['provider_attempts'], 1)
        self.assertIsNone(run.snapshot()['token_usage'])

    def test_repeated_timeouts_stop_after_two_attempts_and_cool_both_models(self):
        with patch('google.genai.Client') as provider, runtime.operation('two_attempts', 3) as run:
            generate = provider.return_value.models.generate_content
            generate.side_effect = ServerError(504, {})
            with self.assertRaises(ServerError):
                _llm.call_json('s', 'u', fixtures.Answer)
            with self.assertRaises(_llm.KeysCoolingDown):
                _llm.call_json('s', 'u', fixtures.Answer)
        self.assertEqual(generate.call_count, 2)
        self.assertEqual(run.snapshot()['provider_attempts'], 2)

    def test_recently_timed_out_primary_is_skipped_on_next_call(self):
        with patch('google.genai.Client') as provider:
            generate = provider.return_value.models.generate_content
            generate.side_effect = [ServerError(504, {}), fixtures.response(), fixtures.response()]
            _llm.call_json('s', 'u', fixtures.Answer)
            _llm.call_json('s', 'u', fixtures.Answer)
        self.assertEqual([call.kwargs['model'] for call in generate.call_args_list],
                         [config.CHEAP_MODEL, config.FALLBACK_MODEL, config.FALLBACK_MODEL])

    def test_timeout_retry_hint_does_not_trigger_fallback(self):
        error = ServerError(504, {'error': {'details': [
            {'@type': 'type.googleapis.com/google.rpc.RetryInfo', 'retryDelay': '120s'}]}})
        with patch('google.genai.Client') as provider:
            provider.return_value.models.generate_content.side_effect = error
            with self.assertRaises(ServerError):
                _llm.call_json('s', 'u', fixtures.Answer)
            with self.assertRaises(_llm.KeysCoolingDown):
                _llm.call_json('s', 'u', fixtures.Answer)
        self.assertEqual(provider.return_value.models.generate_content.call_count, 1)

    def test_identical_fallback_does_not_repeat_slow_model(self):
        with patch.object(config, 'FALLBACK_MODEL', config.CHEAP_MODEL), patch('google.genai.Client') as provider:
            provider.return_value.models.generate_content.side_effect = ServerError(504, {})
            with self.assertRaises(ServerError):
                _llm.call_json('s', 'u', fixtures.Answer)
        self.assertEqual(provider.return_value.models.generate_content.call_count, 1)

    def test_fallback_local_cap_preserves_original_timeout_and_does_not_cycle_keys(self):
        with patch('google.genai.Client') as provider:
            provider.return_value.models.generate_content.side_effect = ServerError(504, {})
            reserve = _llm._reserve
            def limited(key, index, model):
                if model == config.FALLBACK_MODEL:
                    raise _llm.LocalQuotaExceeded('Local fallback allowance used')
                return reserve(key, index, model)
            with patch.object(_llm, '_reserve', side_effect=limited), self.assertRaises(ServerError) as raised:
                _llm.call_json('s', 'u', fixtures.Answer)
        self.assertEqual(raised.exception.code, 504)
        self.assertEqual(provider.call_count, 1)
        self.assertEqual(provider.return_value.models.generate_content.call_count, 1)

    def test_429_does_not_change_models(self):
        with patch('google.genai.Client') as provider:
            generate = provider.return_value.models.generate_content
            generate.side_effect = [ClientError(429, {}), fixtures.response()]
            _llm.call_json('s', 'u', fixtures.Answer)
        self.assertEqual([call.kwargs['model'] for call in generate.call_args_list], [config.CHEAP_MODEL] * 2)

    def test_primary_is_probed_again_after_transient_cooldown_expires(self):
        with patch.object(config, 'AI_MODEL_COOLDOWN_SECONDS', 30), patch('google.genai.Client') as provider, patch.object(_llm.time, 'monotonic', return_value=1000):
            generate = provider.return_value.models.generate_content
            generate.side_effect = [ServerError(504, {}), fixtures.response(), fixtures.response()]
            _llm.call_json('s', 'u', fixtures.Answer)
            with patch.object(_llm.time, 'monotonic', return_value=1031):
                _llm.call_json('s', 'u', fixtures.Answer)
        self.assertEqual([call.kwargs['model'] for call in generate.call_args_list],
                         [config.CHEAP_MODEL, config.FALLBACK_MODEL, config.CHEAP_MODEL])

    def test_structured_calls_disable_automatic_function_calling_and_hidden_retries(self):
        with patch('google.genai.Client') as provider:
            provider.return_value.models.generate_content.return_value = fixtures.response()
            _llm.call_json('s', 'u', fixtures.Answer)
        self.assertEqual(provider.call_args.kwargs['http_options']['retry_options']['attempts'], 1)
        request = provider.return_value.models.generate_content.call_args.kwargs['config']
        self.assertEqual(request['automatic_function_calling'], {'disable': True})

    def test_vision_deadline_defaults_to_sixty_seconds_and_is_bounded(self):
        with patch.dict('os.environ', {}, clear=True), patch('dotenv.load_dotenv'):
            import runpy
            loaded = runpy.run_path(config.__file__)
        self.assertEqual(loaded['AI_VISION_TIMEOUT_MS'], 60000)
        for value, expected in [('1', 10000), ('90000', 60000), ('45000', 45000)]:
            with patch.dict('os.environ', {'AI_VISION_TIMEOUT_MS': value}, clear=True), patch('dotenv.load_dotenv'):
                self.assertEqual(runpy.run_path(config.__file__)['AI_VISION_TIMEOUT_MS'], expected)

    def test_provider_schema_is_compact_but_local_validation_stays_strict(self):
        original = analyst.Findings.model_json_schema()
        wire = provider_schema(analyst.Findings)
        self.assertEqual(original, analyst.Findings.model_json_schema())
        encoded = json.dumps(wire)
        for keyword in ('$defs', '$ref', 'maxItems', 'maxLength', 'minLength', 'default', 'title'):
            # "title" can occur inside the evidence enum; only schema keys count.
            self.assertNotIn('"' + keyword + '":', encoded)
        finding = wire['properties']['findings']['items']
        self.assertEqual(set(finding['required']), set(finding['properties']))
        self.assertEqual(finding['properties']['relevance']['enum'], ['match', 'uncertain', 'mismatch'])
        with self.assertRaises(ValidationError):
            analyst.EvidenceQuote(field='title', text='x' * 241)
        with self.assertRaises(ValidationError):
            analyst.Findings(findings=[fixtures.finding(listing())] * 13)

    def test_every_active_response_contract_has_a_supported_provider_schema(self):
        for model in (analyst.Findings, copilot.ChatPlan, search_intent.SearchIntent, lens.LensIdentity, lens.LensBatch):
            with self.subTest(model=model.__name__):
                wire = provider_schema(model)
                self.assertEqual(wire['type'], 'object')
                self.assertNotIn('"$ref"', json.dumps(wire))

    def test_real_sdk_serializes_compact_request_and_parses_provider_metadata(self):
        row = listing()
        answer = analyst.Findings(findings=[fixtures.finding(row)])
        received = []

        def transport(request):
            self.assertEqual(request.url.host, 'generativelanguage.googleapis.com')
            self.assertEqual(int(request.headers['X-Server-Timeout']), min(config.AI_ANALYSIS_TIMEOUT_MS, config.AI_CALL_TOTAL_TIMEOUT_MS) // 1000)
            body = json.loads(request.content)
            received.append(body)
            return httpx.Response(200, json={'candidates': [{'content': {'role': 'model', 'parts': [{'text': answer.model_dump_json()}]}, 'finishReason': 'STOP'}],
                'usageMetadata': {'promptTokenCount': 40, 'candidatesTokenCount': 30, 'totalTokenCount': 70}})

        def client(**kwargs):
            kwargs['http_options']['client_args'] = {'transport': httpx.MockTransport(transport)}
            return _real_client(**kwargs)

        with patch('httpx.Client.send', _real_send), patch('google.genai.Client', side_effect=client), runtime.operation('sdk_contract', 1) as run:
            result = analyst.analyze_shortlist([row], 'Labubu Macaron')
        self.assertEqual(len(received), 1)
        self.assertEqual(received[0]['generationConfig']['responseJsonSchema'], provider_schema(analyst.Findings))
        thinking = received[0]['generationConfig']['thinkingConfig']
        self.assertEqual(thinking.get('thinkingLevel', thinking.get('thinking_level')).lower(), 'low')
        self.assertEqual(result.statuses[analyst.offer_id(row)], 'ai_completed')
        self.assertEqual(run.snapshot()['token_usage']['total_token_count'], 70)
        self.assertEqual(run.snapshot()['stages']['analyst']['state'], 'completed')

    def test_missing_timezone_database_does_not_block_provider_request_or_health(self):
        with patch.object(_llm, 'ZoneInfo', side_effect=ZoneInfoNotFoundError('missing')), patch('google.genai.Client') as provider, runtime.operation('timezone_test', 1) as run:
            provider.return_value.models.generate_content.return_value = fixtures.response()
            self.assertTrue(_llm.call_json('s', 'u', fixtures.Answer).ok)
            health = api.health()
        self.assertEqual(run.snapshot()['provider_attempts'], 1)
        self.assertEqual(health['agent_runtime']['quota_reset_mode'], 'rolling_24h')
        self.assertIsNone(health['agent_runtime']['attempts_observed_today'])
        self.assertEqual(health['agent_runtime']['attempts_observed_last_24h'], 1)

    def test_timezone_fallback_still_enforces_rolling_daily_allowance(self):
        with patch.object(_llm, 'ZoneInfo', side_effect=ZoneInfoNotFoundError('missing')), patch.object(config, 'GEMINI_KEY_PROJECTS', ('same',) * 5), patch.object(config, 'GEMINI_LOCAL_RPD', 1), patch('google.genai.Client') as provider:
            provider.return_value.models.generate_content.return_value = fixtures.response()
            _llm.call_json('s', 'u', fixtures.Answer)
            for bucket in _llm._limits.values():
                bucket['minute'].clear()
            with self.assertRaises(_llm.LocalQuotaExceeded):
                _llm.call_json('s', 'u', fixtures.Answer)
        self.assertEqual(provider.return_value.models.generate_content.call_count, 1)

    def test_tzdata_supports_system_without_timezone_files(self):
        result = subprocess.run([sys.executable, '-B', '-c',
            "import zoneinfo; zoneinfo.reset_tzpath(()); zoneinfo.ZoneInfo.clear_cache(); print(zoneinfo.ZoneInfo('America/Los_Angeles').key)"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), 'America/Los_Angeles')

    def test_400_error_is_categorized_without_recording_secrets_or_prompt(self):
        exc = ClientError(400, {'error': {'message': 'response_json_schema invalid; fake-2; PRIVATE RECEIPT'}})
        with patch('google.genai.Client') as provider, runtime.operation('bad_schema', 2) as run:
            provider.return_value.models.generate_content.side_effect = exc
            with self.assertRaises(ClientError):
                _llm.call_json('private system prompt', 'PRIVATE RECEIPT', fixtures.Answer)
            report = run.snapshot()
        self.assertEqual(provider.return_value.models.generate_content.call_count, 1)
        error = report['stages']['manager']['error']
        self.assertEqual(error['code'], 400)
        self.assertEqual(error['category'], 'response_schema')
        encoded = json.dumps(report)
        for private in ('fake-2', 'PRIVATE RECEIPT', 'private system prompt'):
            self.assertNotIn(private, encoded)

    def test_planner_success_cannot_mask_analyst_failure(self):
        row = listing()
        def planner(*args, **kwargs):
            runtime.record('manager', 'ai_completed')
            return search_intent.SearchIntent(queries=['Labubu Macaron'], planner='gemini')
        with patch.object(api, 'plan_search', side_effect=planner), patch.object(api.marketplace_service, 'search_many', return_value=([row.model_dump()], {'carousell': 1}, [])), patch.object(analyst, 'call_json', side_effect=ClientError(400, {})), patch.object(api.notify, 'send_telegram_alert') as alert:
            result = api.search(api.SearchRequest(query='Labubu Macaron', planning_mode='ai', pricing_mode='auto', marketplaces=['carousell']))
        self.assertEqual(result.ai_mode, 'deterministic')
        self.assertEqual(result.ai_status['state'], 'invalid_request')
        self.assertEqual(result.ai_status['code'], 400)
        self.assertEqual(result.agent_run['stages']['manager']['state'], 'completed')
        self.assertEqual(result.agent_run['stages']['analyst']['state'], 'failed')
        self.assertFalse(result.decisions[0].is_profitable)
        alert.assert_not_called()

    def test_truncated_output_preserves_token_usage_and_is_not_ai_success(self):
        response = fixtures.response('')
        response.candidates = [SimpleNamespace(finish_reason='MAX_TOKENS')]
        with patch('google.genai.Client') as provider, runtime.operation('truncated', 1) as run:
            provider.return_value.models.generate_content.return_value = response
            with self.assertRaises(_llm.InvalidAIResponse):
                _llm.call_json('s', 'u', fixtures.Answer)
        report = run.snapshot()
        self.assertEqual(report['token_usage']['total_token_count'], 25)
        self.assertEqual(report['stages']['manager']['error']['state'], 'output_truncated')
        self.assertFalse(report['ai_completed'])

    def test_compact_wire_schema_does_not_accept_invalid_local_values(self):
        response = fixtures.response('{"findings":[{"offer_id":"x","relevance":"invented"}]}')
        with patch('google.genai.Client') as provider, runtime.operation('invalid_fields', 1) as run:
            provider.return_value.models.generate_content.return_value = response
            with self.assertRaises(_llm.InvalidAIResponse):
                _llm.call_json('s', 'u', analyst.Findings)
        self.assertEqual(run.snapshot()['stages']['manager']['error']['state'], 'invalid_response')

    def test_rejected_findings_are_reported_as_invalid_response(self):
        row = listing()
        bad = fixtures.finding(row)
        bad.evidence[0].text = 'invented evidence'
        with patch.object(analyst, 'call_json', return_value=analyst.Findings(findings=[bad])), runtime.operation('unsupported_quote', 1) as run:
            analyst.analyze_shortlist([row], 'Labubu Macaron')
        self.assertEqual(runtime.evidence_status(run.snapshot())['state'], 'invalid_response')

    def test_missing_credentials_are_visible_not_a_generic_local_success(self):
        row = listing()
        with patch.object(config, 'GEMINI_API_KEY', ''), patch.object(config, 'GEMINI_API_KEYS', ()), patch.object(api.marketplace_service, 'search_many', return_value=([row.model_dump()], {'carousell': 1}, [])), patch('google.genai.Client') as provider:
            result = api.search(api.SearchRequest(query='Labubu Macaron', pricing_mode='auto', marketplaces=['carousell']))
        provider.assert_not_called()
        self.assertEqual(result.ai_status['state'], 'not_configured')
        self.assertIn('.env', result.ai_status['message'])

    def test_partial_shortlist_is_explicit_and_cached_work_uses_zero_requests(self):
        rows = [listing(id=str(i), price=40+i) for i in range(10)]
        def generate(*args, **kwargs):
            source = json.loads(args[1])['listings']
            return analyst.Findings(findings=[analyst.Finding(offer_id=item['offer_id'], relevance='match', evidence=[{'field': 'title', 'text': item['title']}]) for item in source])
        req = api.SearchRequest(query='Labubu Macaron', pricing_mode='manual', resale_estimate=100, marketplaces=['carousell'])
        with patch.object(api.marketplace_service, 'search_many', return_value=([row.model_dump() for row in rows], {'carousell': 10}, [])), patch.object(analyst, 'call_json', side_effect=generate), patch.object(api.notify, 'send_telegram_alert'):
            first = api.search(req)
            second = api.search(req)
            third = api.search(req)
        self.assertEqual(first.ai_status['state'], 'partial')
        self.assertEqual(sum(item.agent_analysis['status'] == 'not_reviewed' for item in first.decisions), 2)
        self.assertEqual(second.ai_status['state'], 'connected')
        self.assertEqual(third.agent_run['provider_attempts'], 0)
        self.assertTrue(third.agent_run['cached_ai_reused'])

    def test_successful_retry_resolves_the_stage_error(self):
        with patch('google.genai.Client') as provider, runtime.operation('retry', 2) as run:
            provider.return_value.models.generate_content.side_effect = [ClientError(429, {}), fixtures.response()]
            _llm.call_json('s', 'u', fixtures.Answer, role='analyst')
            runtime.record('analyst', 'ai_completed')
        stage = run.snapshot()['stages']['analyst']
        self.assertEqual(stage['state'], 'completed')
        self.assertIsNone(stage['error'])
        self.assertEqual(stage['provider_attempts'], 2)

    def test_extra_backup_and_project_mapping_warnings_do_not_expose_keys(self):
        with patch.object(config, 'GEMINI_API_KEYS', tuple(f'fake-{i}' for i in range(1, 7))):
            status = _llm.runtime_status()
        self.assertEqual(status['configured_key_count'], 6)
        self.assertEqual(len(status['configuration_warnings']), 2)
        self.assertNotIn('fake-', json.dumps(status))

    def test_rolling_daily_fallback_releases_only_expired_attempts(self):
        with patch.object(_llm, 'ZoneInfo', side_effect=ZoneInfoNotFoundError('missing')), patch.object(config, 'GEMINI_LOCAL_RPD', 1), patch.object(_llm.time, 'monotonic', return_value=1000), patch('google.genai.Client') as provider:
            provider.return_value.models.generate_content.return_value = fixtures.response()
            _llm.call_json('s', 'u', fixtures.Answer)
            with patch.object(_llm.time, 'monotonic', return_value=1000 + 86401):
                _llm.call_json('s', 'u', fixtures.Answer)
                self.assertEqual(_llm.runtime_status()['attempts_observed_last_24h'], 1)
        self.assertEqual(provider.return_value.models.generate_content.call_count, 2)

    def test_cached_findings_with_new_failed_work_stay_partial(self):
        with runtime.operation('mixed', 1) as run:
            runtime.record('analyst', 'cache_reused', count=2)
            runtime.record('analyst', 'local_fallback', error={'state': 'timeout', 'message': 'Gemini timed out.'})
        status = runtime.evidence_status(run.snapshot())
        self.assertEqual(status['state'], 'partial')
        self.assertEqual(status['error']['state'], 'timeout')

    def test_empty_market_pool_skips_ai_instead_of_claiming_failure(self):
        with patch.object(api.marketplace_service, 'search_many', return_value=([], {'carousell': 0}, [])), patch('google.genai.Client') as provider:
            result = api.search(api.SearchRequest(query='LEGO 75192', pricing_mode='auto', marketplaces=['carousell']))
        provider.assert_not_called()
        self.assertEqual(result.ai_status['state'], 'skipped')
        self.assertEqual(result.agent_run['provider_attempts'], 0)

    def test_thinking_config_does_not_get_sent_to_unverified_models(self):
        with patch.object(config, 'CHEAP_MODEL', 'custom-model'), patch('google.genai.Client') as provider:
            provider.return_value.models.generate_content.return_value = fixtures.response()
            _llm.call_json('s', 'u', fixtures.Answer)
        self.assertNotIn('thinking_config', provider.return_value.models.generate_content.call_args.kwargs['config'])

    def test_setup_checker_is_offline_unless_live_is_explicit(self):
        with patch.object(sys, 'argv', ['check_gemini.py']), patch('builtins.print') as output, patch('google.genai.Client') as provider:
            code = check_gemini.main()
        provider.assert_not_called()
        self.assertEqual(code, 0)
        payload = json.loads(output.call_args.args[0])
        self.assertFalse(payload['live_request_sent'])
        self.assertNotIn('fake-', json.dumps(payload))

    def test_live_checker_verifies_schema_and_grounding_with_one_attempt(self):
        result = analyst.Findings(findings=[analyst.Finding(offer_id='diagnostic:1', relevance='match', evidence=[
            {'field': 'title', 'text': 'LEGO Star Wars 75192 UCS Millennium Falcon'}])])
        with patch.object(sys, 'argv', ['check_gemini.py', '--live']), patch('builtins.print') as output, patch('google.genai.Client') as provider:
            provider.return_value.models.generate_content.return_value = fixtures.response(result.model_dump_json())
            code = check_gemini.main()
        payload = json.loads(output.call_args.args[0])
        self.assertEqual(code, 0)
        self.assertTrue(payload['verified'])
        self.assertEqual(payload['agent_run']['provider_attempts'], 1)
        self.assertEqual(payload['agent_run']['token_usage']['total_token_count'], 25)
        self.assertEqual(config.GEMINI_MAX_ATTEMPTS, 2)

    def test_live_checker_does_not_cycle_keys_or_leak_provider_messages(self):
        with patch.object(sys, 'argv', ['check_gemini.py', '--live']), patch('builtins.print') as output, patch('google.genai.Client') as provider:
            provider.return_value.models.generate_content.side_effect = ClientError(400, {'error': {'message': 'response_schema fake-2 PRIVATE DATA'}})
            code = check_gemini.main()
        payload = json.loads(output.call_args.args[0])
        self.assertEqual(code, 1)
        self.assertFalse(payload['verified'])
        self.assertEqual(provider.return_value.models.generate_content.call_count, 1)
        self.assertEqual(payload['error']['category'], 'response_schema')
        self.assertNotIn('fake-', json.dumps(payload))
        self.assertNotIn('PRIVATE DATA', json.dumps(payload))

    def test_checker_uses_configured_deadline_and_reports_after_run_counts(self):
        with patch.object(config, 'AI_ANALYSIS_TIMEOUT_MS', 45000), patch.object(sys, 'argv', ['check_gemini.py', '--live']), patch('builtins.print') as output, patch('google.genai.Client') as provider:
            provider.return_value.models.generate_content.side_effect = ClientError(400, {})
            check_gemini.main()
        payload = json.loads(output.call_args.args[0])
        self.assertEqual(provider.call_args.kwargs['http_options']['timeout'], 45000)
        self.assertEqual(payload['deadline_seconds'], 45)
        self.assertEqual(payload['configuration']['attempts_observed_last_24h'], 1)
        self.assertEqual(payload['configuration']['max_attempts_per_call'], 2)
        self.assertGreaterEqual(payload['elapsed_seconds'], 0)

    def test_checker_explicit_sixty_seconds_overrides_old_thirty_second_setting(self):
        with patch.object(config, 'AI_ANALYSIS_TIMEOUT_MS', 30000), patch.object(sys, 'argv', ['check_gemini.py', '--live', '--timeout-seconds', '60']), patch('builtins.print') as output, patch('google.genai.Client') as provider:
            provider.return_value.models.generate_content.side_effect = ClientError(400, {})
            check_gemini.main()
        payload = json.loads(output.call_args.args[0])
        self.assertEqual(provider.call_args.kwargs['http_options']['timeout'], 60000)
        self.assertEqual(payload['deadline_seconds'], 60)
        self.assertEqual(payload['agent_run']['events'][0]['timeout_ms'], 60000)
        self.assertEqual(provider.return_value.models.generate_content.call_count, 1)

    def test_checker_timeout_is_bounded_and_invalid_values_send_nothing(self):
        for value in ('0', '9', '61', '9999', 'nan'):
            with self.subTest(value=value), patch.object(sys, 'argv', ['check_gemini.py', '--live', '--timeout-seconds', value]), patch('sys.stderr'), patch('google.genai.Client') as provider:
                with self.assertRaises(SystemExit):
                    check_gemini.main()
                provider.assert_not_called()

    def test_504_is_not_retried_and_has_no_fabricated_usage(self):
        from google.genai.errors import ServerError
        with patch.object(sys, 'argv', ['check_gemini.py', '--live', '--timeout-seconds', '60']), patch('builtins.print') as output, patch('google.genai.Client') as provider:
            provider.return_value.models.generate_content.side_effect = ServerError(504, {})
            code = check_gemini.main()
        payload = json.loads(output.call_args.args[0])
        self.assertEqual(code, 1)
        self.assertEqual(provider.return_value.models.generate_content.call_count, 1)
        self.assertFalse(payload['verified'])
        self.assertEqual(payload['error']['code'], 504)
        self.assertIsNone(payload['agent_run']['token_usage'])


if __name__ == '__main__':
    unittest.main()
