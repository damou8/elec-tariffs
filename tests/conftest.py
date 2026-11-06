"""Shared fixtures: recorded answers, an HTTP session and a fake HTTP server."""

from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from multidict import CIMultiDictProxy, MultiMapping

FIXTURES = Path(__file__).parent / "fixtures"

type Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]


def load_fixture(name: str) -> str:
    """Return a recorded answer as text."""
    return (FIXTURES / name).read_text(encoding="utf-8")


@dataclass(frozen=True)
class Request:
    """What the fake server received."""

    method: str
    path: str
    query: MultiMapping[str]
    headers: CIMultiDictProxy[str]
    body: str


@dataclass
class FakeServer:
    """A local HTTP server that answers each path with queued handlers."""

    url: str = ""
    requests: list[Request] = field(default_factory=list)
    _routes: dict[tuple[str, str], list[Handler]] = field(default_factory=dict)

    def add(self, method: str, path: str, handler: Handler) -> None:
        """Answer the next `method` request to `path` with `handler`."""
        self._routes.setdefault((method, path), []).append(handler)

    def json(self, method: str, path: str, payload: object, status: int = 200) -> None:
        """Answer the next request to `path` with JSON."""

        async def handler(_: web.Request) -> web.Response:
            return web.json_response(payload, status=status)

        self.add(method, path, handler)

    def text(self, method: str, path: str, body: str, status: int = 200) -> None:
        """Answer the next request to `path` with text."""

        async def handler(_: web.Request) -> web.Response:
            return web.Response(text=body, status=status)

        self.add(method, path, handler)

    def calls(self, path: str) -> list[Request]:
        """Return the requests received on `path`."""
        return [request for request in self.requests if request.path == path]

    async def dispatch(self, request: web.Request) -> web.StreamResponse:
        """Record the request and answer with the next handler queued for it."""
        self.requests.append(
            Request(
                method=request.method,
                path=request.path,
                query=request.query,
                headers=request.headers,
                body=await request.text(),
            )
        )
        handlers = self._routes.get((request.method, request.path))
        if not handlers:
            return web.Response(status=599, text="No answer queued")
        return await handlers.pop(0)(request)


@pytest.fixture
async def session() -> AsyncIterator[aiohttp.ClientSession]:
    """Give an HTTP session, closed after the test."""
    async with aiohttp.ClientSession() as client:
        yield client


@pytest.fixture
async def server() -> AsyncIterator[FakeServer]:
    """Run a fake HTTP server for the test."""
    fake = FakeServer()
    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", fake.dispatch)
    async with TestServer(app) as test_server:
        fake.url = str(test_server.make_url("")).rstrip("/")
        yield fake


@pytest.fixture
def closed_url() -> str:
    """Give an address where nothing listens."""
    return "http://127.0.0.1:9"
