import logging
from contextlib import AsyncExitStack
from typing import Callable, Sequence

from databricks.sdk import WorkspaceClient
from databricks_openai.agents import McpServer

from agent_server.utils import build_mcp_url

logger = logging.getLogger(__name__)


def init_mcp_server(
    *,
    url: str,
    name: str,
    workspace_client: WorkspaceClient | None = None,
) -> McpServer:
    """Construct a Databricks MCP server.

    The returned server is not yet connected — pass it (or a factory that
    returns one) into ``connect_mcp_servers`` to enter the async context.

    Args:
        url: Either a path beginning with ``/`` (resolved against the
            workspace host, e.g. ``/api/2.0/mcp/genie/<id>``) or a fully
            qualified URL.
        name: Human-readable name shown in traces and logs.
        workspace_client: Optional ``WorkspaceClient`` for authentication.
            For per-user (OBO) auth, pass ``get_user_workspace_client()``
            from inside the request handler — never at module load.
    """
    if workspace_client is None:
        workspace_client = WorkspaceClient()
    return McpServer(
        url=build_mcp_url(url, workspace_client=workspace_client),
        name=name,
        workspace_client=workspace_client,
    )


async def connect_mcp_servers(
    stack: AsyncExitStack,
    factories: Sequence[Callable[[], McpServer]],
) -> list[McpServer]:
    """Connect a list of MCP servers, surviving individual failures.

    Each factory is invoked and the resulting server is entered through
    ``stack``. If any single server fails to construct or connect, the
    failure is logged with a stack trace (so signature mismatches and
    other quiet-fallback bugs surface in ``databricks apps logs``) and
    the remaining servers continue to load. The agent loop is expected
    to keep running with whatever connected — a missing server may
    degrade or remove a capability rather than fail the whole request.

    Args:
        stack: Caller-owned ``AsyncExitStack`` that owns cleanup.
        factories: Zero-arg callables, each returning a fresh
            ``McpServer`` (typically constructed with ``init_mcp_server``).

    Returns:
        The successfully-connected servers in factory order.
    """
    connected: list[McpServer] = []
    for factory in factories:
        server: McpServer | None = None
        try:
            server = factory()
            entered = await stack.enter_async_context(server)
            connected.append(entered)
        except Exception:
            identity = getattr(server, "name", None) or "<construction-failed>"
            logger.warning(
                "MCP server %r failed to initialize; continuing without it.",
                identity,
                exc_info=True,
            )
    return connected
