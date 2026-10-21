"""날짜 해석, 비동기 호출, 초기화 실패의 회귀 검사. 외부 API는 사용하지 않는다."""
import asyncio
import os
from pathlib import Path
import sys
import threading
import unittest
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import patch

os.environ['LANGSMITH_TRACING'] = 'false'
os.environ['LANGCHAIN_TRACING_V2'] = 'false'
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent.targets import resolve
from agent.openai_llm import OpenAIFastLLM, OpenAIQualityLLM
import agent.mcp_client as mcp_module
import server


class DateTargetTests(unittest.TestCase):
    def test_date_is_not_a_target(self):
        result = resolve('payments-service 2026-10-20 상태 확인')
        self.assertEqual(result.status, 'resolved')
        self.assertEqual(result.requested_names, ['payments-service'])
        self.assertEqual(result.resolved_systems, ['payments-service'])

    def test_date_does_not_hide_unknown_system(self):
        result = resolve('2026-10-20 payments-service billing-service 상태 확인')
        self.assertEqual(result.status, 'needs_clarification')
        self.assertEqual(result.unresolved_names, ['billing-service'])

    def test_date_alone_still_needs_target(self):
        self.assertEqual(resolve('2024-02-29 상태 확인').status, 'needs_clarification')

    def test_invalid_date_is_not_silently_dropped(self):
        self.assertIn('2026-02-30', resolve('payments-service 2026-02-30 상태').unresolved_names)

    def test_numeric_service_suffix_kept(self):
        result = resolve('web-01과 app-2026-10-20 상태 확인')
        self.assertEqual(result.resolved_systems, ['web-01'])
        self.assertEqual(result.unresolved_names, ['app-2026-10-20'])


class NonBlockingTests(unittest.IsolatedAsyncioTestCase):
    async def check_call(self, model, invoke):
        started, release = threading.Event(), threading.Event()
        main_thread = threading.get_ident()
        used_threads, callbacks = [], []
        def http_call(*args, **kwargs):
            used_threads.append(threading.get_ident())
            started.set()
            if not release.wait(2):
                raise RuntimeError('Event loop could not release the HTTP worker')
            return '응답', {'tokens': 1, 'usd': 0}
        model._chat = http_call
        task = asyncio.create_task(invoke(model, lambda text: callbacks.append((text, threading.get_ident()))))
        try:
            for _ in range(100):
                if started.is_set():
                    break
                await asyncio.sleep(.01)
            self.assertTrue(started.is_set())
            self.assertFalse(task.done())
            release.set()
            text, usage = await asyncio.wait_for(task, 3)
            self.assertEqual(text, '응답')
            self.assertTrue(usage['prompt'])
            self.assertNotEqual(used_threads, [main_thread])
            self.assertEqual(callbacks, [('응답', main_thread)])
        finally:
            release.set()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_chat_allows_other_async_work(self):
        await self.check_call(OpenAIFastLLM('test', 0),
                              lambda m, cb: m.chat('안녕', [], on_token=cb))

    async def test_legacy_write_allows_other_async_work(self):
        await self.check_call(OpenAIQualityLLM('test', 0),
                              lambda m, cb: m.write('질문', [], 1, None, on_token=cb))


class StartupTests(unittest.IsolatedAsyncioTestCase):
    async def check_startup(self, stage=None):
        events = []
        class Client:
            async def start(self):
                events.append('mcp_start')
                if stage == 'mcp':
                    raise RuntimeError('start failed')
            async def stop(self):
                events.append('mcp_stop')
        class Store:
            def __init__(self):
                if stage == 'store':
                    raise RuntimeError('store failed')
                events.append('store_open')
            def close(self):
                events.append('store_close')
        def models():
            if stage == 'models':
                raise RuntimeError('models failed')
            return 'test', SimpleNamespace(name='fast'), SimpleNamespace(name='quality')
        def graph(deps):
            if stage == 'graph':
                raise RuntimeError('graph failed')
            return object()
        with patch.object(server, 'MCPClient', Client), patch.object(server, 'SqliteSessionStore', Store), \
             patch.object(server, 'build_llms', models), patch.object(server, 'build_graph', graph):
            if stage:
                with self.assertRaises(RuntimeError):
                    async with server.lifespan(SimpleNamespace(state=SimpleNamespace())):
                        self.fail('Failed startup must not yield')
            else:
                async with server.lifespan(SimpleNamespace(state=SimpleNamespace())):
                    events.append('served')
        self.assertEqual(events.count('mcp_stop'), 1)
        self.assertEqual(events.count('store_close'), int(stage not in ('mcp', 'store')))
        return events

    async def test_mcp_start_failure_cleaned(self):
        await self.check_startup('mcp')

    async def test_store_failure_closes_mcp(self):
        await self.check_startup('store')

    async def test_model_failure_closes_resources(self):
        await self.check_startup('models')

    async def test_graph_failure_closes_resources(self):
        await self.check_startup('graph')

    async def test_normal_shutdown(self):
        self.assertIn('served', await self.check_startup())


class MCPPartialStartupTests(unittest.IsolatedAsyncioTestCase):
    async def check_partial(self, cancel=False):
        events = []
        @asynccontextmanager
        async def transport(params):
            name = Path(params.args[0]).stem
            events.append('open:' + name)
            try:
                yield name, name
            finally:
                events.append('close:' + name)
        class Session:
            def __init__(self, read, write):
                self.name = read
            async def __aenter__(self):
                return self
            async def __aexit__(self, *args):
                events.append('session_close:' + self.name)
            async def initialize(self):
                if self.name == 'second':
                    if cancel:
                        raise asyncio.CancelledError()
                    raise RuntimeError('second server failed')
        client = mcp_module.MCPClient(server_scripts={'one': Path('first.py'), 'two': Path('second.py')})
        with patch.object(mcp_module, 'stdio_client', transport), patch.object(mcp_module, 'ClientSession', Session):
            with self.assertRaises(asyncio.CancelledError if cancel else RuntimeError):
                await client.start()
            self.assertEqual(events[-4:], ['session_close:second', 'close:second', 'session_close:first', 'close:first'])
            self.assertEqual(client.sessions, {})
            self.assertIsNone(client._stack)
            before = list(events)
            await client.stop()
            self.assertEqual(events, before)

    async def test_second_server_failure_closes_first(self):
        await self.check_partial()

    async def test_start_cancellation_closes_connections(self):
        await self.check_partial(cancel=True)


if __name__ == '__main__':
    unittest.main()
