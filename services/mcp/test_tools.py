"""Offline MCP contract tests; never contacts OAuth or production Horolog."""
import os
import unittest
from unittest.mock import AsyncMock, patch

for key, value in {
    'HOROLOG_OAUTH_ISSUER': 'https://auth.example.test/',
    'HOROLOG_OAUTH_CLIENT_ID': 'test-client',
    'HOROLOG_OAUTH_SUBJECT': 'test-subject',
    'HOROLOG_OAUTH_JWKS_URL': 'https://auth.example.test/jwks',
    'HOROLOG_MCP_RESOURCE_URL': 'https://mcp.example.test/mcp',
}.items():
    os.environ[key] = value

import server


class ToolsTests(unittest.IsolatedAsyncioTestCase):
    async def test_config_uses_public_resource_url(self):
        self.assertEqual(server.MCP_RESOURCE_URL, 'https://mcp.example.test/mcp')
        self.assertEqual(server.resource_origin, 'https://mcp.example.test')
        self.assertIn('mcp.example.test', server.allowed_hosts)
        self.assertEqual(server.mcp._token_verifier.audience, server.MCP_RESOURCE_URL)
        with patch.dict(os.environ, {'HOROLOG_OAUTH_AUDIENCE': 'alternate-audience'}):
            self.assertEqual(server.OIDCTokenVerifier().audience, 'alternate-audience')

    async def test_partial_update_preserves_null_and_omission(self):
        with patch.object(server, 'horolog_request', new_callable=AsyncMock) as request:
            await server.update_task('abc', server.TaskChanges(color=None, tags=[]))
            request.assert_awaited_once_with('PATCH', '/api/intents/abc', {'color': None, 'tags': []})
            request.reset_mock()
            await server.update_task('abc', server.TaskChanges(title='New', priority=2, allowed_weekdays=[0, 6]))
            request.assert_awaited_once_with('PATCH', '/api/intents/abc', {
                'title': 'New', 'priority': 2, 'allowed_weekdays': [0, 6],
            })
            with self.assertRaises(ValueError):
                await server.update_task('abc', server.TaskChanges())

    async def test_routes(self):
        with patch.object(server, 'horolog_request', new_callable=AsyncMock) as request:
            for tool, method, path in [
                (server.get_task, 'GET', '/api/intents/abc'),
                (server.delete_task, 'DELETE', '/api/intents/abc'),
                (server.complete_task, 'POST', '/api/intents/abc/complete'),
                (server.uncomplete_task, 'DELETE', '/api/intents/abc/complete'),
            ]:
                await tool('abc')
                request.assert_awaited_with(method, path)
            await server.get_availability(60, 3)
            request.assert_awaited_with('GET', '/api/availability?minutes=60&days=3')

    async def test_weekly_create(self):
        with patch.object(server, 'horolog_request', new_callable=AsyncMock) as request:
            await server.create_task('Habit', 45, allowed_weekdays=[0, 6])
            payload = request.call_args.args[2]
            self.assertEqual(payload['minutes_per_period'], 90)
            self.assertEqual(payload['min_chunk_minutes'], 45)
            self.assertEqual(payload['allowed_weekdays'], [0, 6])
            self.assertEqual(payload['period_days'], 7)

    async def test_summary_keeps_unmet_demand(self):
        plan = {'origin': '2026-10-20T00:00:00-07:00', 'horizon_days': 7, 'complete': False,
                'blocks': [{'intent_id': 'a', 'title': 'A', 'start': '2026-10-20T09:00:00-07:00',
                            'end': '2026-10-20T09:45:00-07:00', 'late_by_minutes': 30}],
                'unmet': [{'intent_id': 'b', 'shortfall_minutes': 60}]}
        with patch.object(server, 'get_schedule', new_callable=AsyncMock, return_value=plan):
            summary = await server.get_schedule_summary()
        self.assertEqual(summary['scheduled_minutes'], 45)
        self.assertEqual(summary['unmet'], plan['unmet'])
        self.assertFalse(summary['complete'])
        self.assertEqual(len(summary['late_blocks']), 1)

    async def test_no_content_delete(self):
        import httpx
        transport = httpx.MockTransport(lambda request: httpx.Response(204))
        factory = httpx.AsyncClient
        with patch.object(server.httpx, 'AsyncClient', side_effect=lambda **kw: factory(transport=transport, **kw)):
            self.assertEqual(await server.delete_task('abc'), {'deleted': True})

    async def test_calendar_block_tools(self):
        with patch.object(server, 'horolog_request', new_callable=AsyncMock) as request:
            await server.create_calendar_block('Conference day', all_day_date='2030-01-15')
            request.assert_awaited_once_with('POST', '/api/calendar-blocks', {
                'title': 'Conference day', 'all_day_date': '2030-01-15',
            })
            request.reset_mock()
            await server.create_calendar_block('Trip', all_day_date='2026-10-27', end_date='2026-10-29')
            self.assertEqual(request.call_args.args[2]['end_date'], '2026-10-29')
            await server.list_calendar_blocks()
            request.assert_awaited_with('GET', '/api/calendar-blocks')
            await server.delete_calendar_block('abc')
            request.assert_awaited_with('DELETE', '/api/calendar-blocks/abc')
        tools = {tool.name: tool for tool in await server.mcp.list_tools()}
        self.assertTrue({'create_calendar_block', 'list_calendar_blocks', 'delete_calendar_block'} <= tools.keys())
        with patch.object(server, 'horolog_request', new_callable=AsyncMock, return_value={'id': 'abc'}) as request:
            await server.mcp.call_tool('create_calendar_block', {
                'title': 'Conference day', 'all_day_date': '2026-10-27',
            })
            request.assert_awaited_once_with('POST', '/api/calendar-blocks', {
                'title': 'Conference day', 'all_day_date': '2026-10-27',
            })

    async def test_retry_keys_and_idle_mode(self):
        with patch.object(server, 'horolog_request', new_callable=AsyncMock) as request:
            await server.create_task('Retry', 60, due='2026-10-30T12:00:00-07:00', request_id='stable-request')
            self.assertEqual(request.call_args.kwargs, {'idempotency_key': 'stable-request'})
            self.assertEqual(request.call_args.args[2]['due'], '2026-10-30T12:00:00-07:00')
            await server.create_calendar_block('Trip', all_day_date='2026-10-27', request_id='stable-trip')
            self.assertEqual(request.call_args.kwargs, {'idempotency_key': 'stable-trip'})
            await server.get_availability(60, 7, mode='idle')
            request.assert_awaited_with('GET', '/api/availability?minutes=60&days=7&mode=idle')

    async def test_edit_block_and_occurrence_tools(self):
        with patch.object(server, 'horolog_request', new_callable=AsyncMock, return_value={'ok': True}) as request:
            await server.mcp.call_tool('update_calendar_block', {
                'block_id': 'abc', 'changes': {'all_day_date': '2026-10-28'},
            })
            request.assert_awaited_with('PATCH', '/api/calendar-blocks/abc', {'all_day_date': '2026-10-28'})
            await server.skip_task_session('abc', '2026-10-27')
            request.assert_awaited_with('POST', '/api/intents/abc/skips/2026-10-27')
            await server.restore_task_session('abc', '2026-10-27')
            request.assert_awaited_with('DELETE', '/api/intents/abc/skips/2026-10-27')
            await server.update_task('abc', server.TaskChanges(session_minutes=45))
            request.assert_awaited_with('PATCH', '/api/intents/abc', {'session_minutes': 45})

    async def test_retry_header_is_transmitted(self):
        import httpx
        seen = []
        def handle(request):
            seen.append(request.headers.get('Idempotency-Key'))
            return httpx.Response(201, json={'id': 'abc'})
        transport = httpx.MockTransport(handle)
        factory = httpx.AsyncClient
        with patch.object(server.httpx, 'AsyncClient', side_effect=lambda **kw: factory(transport=transport, **kw)):
            await server.create_task('Retry', 60, request_id='unique-retry')
        self.assertEqual(seen, ['unique-retry'])

    async def test_registered_tools_and_patch_schema(self):
        tools = {tool.name: tool for tool in await server.mcp.list_tools()}
        self.assertTrue({'get_task', 'update_task', 'delete_task', 'complete_task', 'uncomplete_task',
                         'get_availability', 'get_schedule_summary'} <= tools.keys())
        self.assertIn('changes', tools['update_task'].inputSchema['properties'])
        with patch.object(server, 'horolog_request', new_callable=AsyncMock, return_value={'ok': True}) as request:
            await server.mcp.call_tool('update_task', {'task_id': 'abc', 'changes': {'color': None}})
            request.assert_awaited_once_with('PATCH', '/api/intents/abc', {'color': None})


if __name__ == '__main__':
    unittest.main()
