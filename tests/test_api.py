"""Tests for the Solax Cloud API client against a real local HTTP server."""

from __future__ import annotations

import aiohttp
from aiohttp import web
import pytest

from custom_components.solax_cloud.api import (
    REALTIME_PATH,
    SolaxCloudClient,
    SolaxCloudConnectionError,
)

from .common import SERIAL, TOKEN, ok_response


@pytest.mark.usefixtures("socket_enabled")
@pytest.mark.parametrize("status", [301, 302, 307, 308])
async def test_regression_redirect_does_not_carry_the_token_away(
    aiohttp_server, status: int
) -> None:
    """Regression: redirects were followed with the tokenId header attached.

    aiohttp keeps custom headers across redirects, even to another host or
    to plain http, so an address that redirects (a web page instead of the
    API host) would have handed the token to the redirect target.
    """
    stolen: list[str | None] = []

    async def api(request: web.Request) -> web.Response:
        raise _redirect(status)

    async def elsewhere(request: web.Request) -> web.Response:
        stolen.append(request.headers.get("tokenId"))
        return web.json_response(ok_response())

    app = web.Application()
    app.router.add_post(REALTIME_PATH, api)
    app.router.add_route("*", "/elsewhere", elsewhere)
    server = await aiohttp_server(app)

    async with aiohttp.ClientSession() as session:
        client = SolaxCloudClient(session, str(server.make_url("/")), TOKEN, SERIAL)
        with pytest.raises(SolaxCloudConnectionError, match="redirect"):
            await client.async_get_realtime_data()

    assert stolen == []


def _redirect(status: int) -> web.HTTPException:
    classes = {
        301: web.HTTPMovedPermanently,
        302: web.HTTPFound,
        307: web.HTTPTemporaryRedirect,
        308: web.HTTPPermanentRedirect,
    }
    return classes[status](location="/elsewhere")
