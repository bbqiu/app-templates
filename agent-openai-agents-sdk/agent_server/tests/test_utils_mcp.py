import logging
from contextlib import AsyncExitStack
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent_server.utils.mcp import connect_mcp_servers


def _fake_server(name: str = "server") -> MagicMock:
    server = MagicMock(name=name)
    # MagicMock's `name` kwarg sets the repr only; set the attribute explicitly
    # so production code reading `server.name` sees the intended identifier.
    server.name = name
    server.__aenter__ = AsyncMock(return_value=server)
    server.__aexit__ = AsyncMock(return_value=None)
    return server


async def test_connects_all_factories_in_order():
    s1, s2, s3 = _fake_server("s1"), _fake_server("s2"), _fake_server("s3")
    factories = [lambda: s1, lambda: s2, lambda: s3]

    async with AsyncExitStack() as stack:
        connected = await connect_mcp_servers(stack, factories)
        assert connected == [s1, s2, s3]
        for s in (s1, s2, s3):
            s.__aenter__.assert_awaited_once()
            s.__aexit__.assert_not_awaited()

    for s in (s1, s2, s3):
        s.__aexit__.assert_awaited_once()


async def test_skips_server_that_raises_during_construction(caplog):
    s1, s3 = _fake_server("s1"), _fake_server("s3")

    def boom() -> MagicMock:
        raise RuntimeError("kaboom")

    factories = [lambda: s1, boom, lambda: s3]

    with caplog.at_level(logging.WARNING, logger="agent_server.utils.mcp"):
        async with AsyncExitStack() as stack:
            connected = await connect_mcp_servers(stack, factories)
        assert connected == [s1, s3]

    failed_records = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(failed_records) == 1, [r.getMessage() for r in caplog.records]
    record = failed_records[0]
    assert "failed to initialize" in record.getMessage()
    # Construction itself raised, so the server identity is the sentinel.
    assert "<construction-failed>" in record.getMessage()
    # Stack trace must be attached so the real exception (e.g. a TypeError
    # from get_user_workspace_client(request)) shows up in logs.
    assert record.exc_info is not None
    assert "kaboom" in record.exc_info[1].args[0]


async def test_skips_server_that_raises_during_connect(caplog):
    good = _fake_server("good")
    bad = _fake_server("my-genie")
    bad.__aenter__ = AsyncMock(side_effect=ConnectionError("403 Forbidden"))

    factories = [lambda: bad, lambda: good]

    with caplog.at_level(logging.WARNING, logger="agent_server.utils.mcp"):
        async with AsyncExitStack() as stack:
            connected = await connect_mcp_servers(stack, factories)
        assert connected == [good]

    record = next(r for r in caplog.records if r.levelno == logging.WARNING)
    assert record.exc_info is not None
    assert isinstance(record.exc_info[1], ConnectionError)
    # Operators scanning `databricks apps logs` need the failing server's
    # identity in the formatted log line — not just the generic message.
    assert "my-genie" in record.getMessage()
    # If `__aenter__` raised, `enter_async_context` must not have registered
    # the server for cleanup — its `__aexit__` should never be awaited.
    bad.__aexit__.assert_not_awaited()


async def test_empty_factories_returns_empty_list():
    async with AsyncExitStack() as stack:
        connected = await connect_mcp_servers(stack, [])
    assert connected == []


@pytest.mark.parametrize(
    "factories_setup",
    [
        # All succeed — returned in factory order.
        ("succeed_succeed", lambda s1, s2: [lambda: s1, lambda: s2], 2),
        # First fails — second still loads.
        (
            "fail_succeed",
            lambda s1, s2: [lambda: (_ for _ in ()).throw(RuntimeError("x")), lambda: s2],
            1,
        ),
        # Both fail — empty list, agent loop should still run.
        (
            "fail_fail",
            lambda s1, s2: [
                lambda: (_ for _ in ()).throw(RuntimeError("x")),
                lambda: (_ for _ in ()).throw(RuntimeError("y")),
            ],
            0,
        ),
    ],
)
async def test_resilient_init_matrix(factories_setup):
    _name, build, expected_len = factories_setup
    s1 = _fake_server("s1")
    s2 = _fake_server("s2")
    factories = build(s1, s2)
    async with AsyncExitStack() as stack:
        connected = await connect_mcp_servers(stack, factories)
    assert len(connected) == expected_len
