"""HTML问答的阻塞检索须在线程池中，事件循环能及时处理其它协程。"""

import asyncio
import threading
from contextlib import contextmanager
from types import SimpleNamespace

from researchlib.web import routes


def test_html_ask_does_not_block_its_event_loop(monkeypatch):
    released = threading.Event()

    class Request:
        app = SimpleNamespace(state=SimpleNamespace(settings=object()))

        async def form(self):
            return {'q': '合成问题'}

    @contextmanager
    def database(_request):
        yield object()

    def start(*args):
        assert released.wait(.5), '检索阻塞时，事件循环应能运行释放协程'
        return {'id': 7}

    monkeypatch.setattr(routes, '_same_origin', lambda r: True)
    monkeypatch.setattr(routes, 'database', database)
    monkeypatch.setattr(routes, 'active_sources', lambda *a: [])
    monkeypatch.setattr(routes, 'resolve_scope', lambda *a: (object(), {}))
    monkeypatch.setattr(routes, 'start', start)

    async def run():
        async def release():
            await asyncio.sleep(.01)
            released.set()

        task = asyncio.create_task(release())
        try:
            result = await routes.ask_submit(Request())
            assert result.status_code == 303 and result.headers['location'] == '/ask/7'
        finally:
            await task

    asyncio.run(run())
