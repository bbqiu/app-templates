import logging
from datetime import datetime
from typing import AsyncGenerator

import litellm
import mlflow
from agents import Agent, Runner, function_tool, set_default_openai_api, set_default_openai_client
from agents.tracing import set_trace_processors
from databricks_openai import AsyncDatabricksOpenAI
from databricks_openai.agents import McpServer
from mlflow.genai.agent_server import invoke, stream
from mlflow.types.responses import (
    ResponsesAgentRequest,
    ResponsesAgentResponse,
    ResponsesAgentStreamEvent,
)

from agent_server.utils import (
    get_session_id,
    process_agent_stream_events,
)

logger = logging.getLogger(__name__)

# NOTE: this will work for all databricks models OTHER than GPT-OSS, which uses a slightly different API
set_default_openai_client(AsyncDatabricksOpenAI())
set_default_openai_api("chat_completions")
set_trace_processors([])  # only use mlflow for trace processing
mlflow.openai.autolog()
logging.getLogger("mlflow.utils.autologging_utils").setLevel(logging.ERROR)
litellm.suppress_debug_info = True


@function_tool
def get_current_time() -> str:
    """Get the current date and time."""
    return datetime.now().isoformat()


def create_agent(mcp_servers: list[McpServer] | None = None) -> Agent:
    return Agent(
        name="Agent",
        instructions="You are a helpful assistant.",
        model="databricks-gpt-5-2",
        tools=[get_current_time],
        mcp_servers=mcp_servers or [],
    )


@invoke()
async def invoke_handler(request: ResponsesAgentRequest) -> ResponsesAgentResponse:
    if session_id := get_session_id(request):
        mlflow.update_current_trace(metadata={"mlflow.trace.session": session_id})
    # To use MCP server tools, replace the `agent = create_agent()` line below
    # with the AsyncExitStack pattern. `connect_mcp_servers` skips servers that
    # fail to initialize (logs the stack trace via `exc_info=True`) so one bad
    # tool does not poison the rest. By default uses service principal
    # credentials via WorkspaceClient(); for OBO (per-user) authentication,
    # swap for `get_user_workspace_client()` inside this handler — see the
    # `add-tools-openai` skill's "OBO" section.
    #
    # from contextlib import AsyncExitStack
    # from databricks.sdk import WorkspaceClient
    # from agent_server.utils.mcp import connect_mcp_servers, init_mcp_server
    #
    # async with AsyncExitStack() as stack:
    #     mcp_servers = await connect_mcp_servers(stack, [
    #         lambda: init_mcp_server(
    #             url="/api/2.0/mcp/functions/system/ai",
    #             name="system.ai UC function MCP server",
    #             workspace_client=WorkspaceClient(),
    #         ),
    #         # add more factories here for additional MCP servers
    #     ])
    #     agent = create_agent(mcp_servers=mcp_servers)
    #     messages = [i.model_dump() for i in request.input]
    #     result = await Runner.run(agent, messages)
    #     return ResponsesAgentResponse(
    #         output=[item.to_input_item() for item in result.new_items]
    #     )
    agent = create_agent()
    messages = [i.model_dump() for i in request.input]
    result = await Runner.run(agent, messages)
    return ResponsesAgentResponse(output=[item.to_input_item() for item in result.new_items])


@stream()
async def stream_handler(
    request: ResponsesAgentRequest,
) -> AsyncGenerator[ResponsesAgentStreamEvent, None]:
    if session_id := get_session_id(request):
        mlflow.update_current_trace(metadata={"mlflow.trace.session": session_id})
    # To use MCP server tools, replace the `agent = create_agent()` line below
    # with the AsyncExitStack pattern. See the `invoke_handler` example above
    # for the full snippet (including OBO swap).
    agent = create_agent()
    messages = [i.model_dump() for i in request.input]
    result = Runner.run_streamed(agent, input=messages)

    async for event in process_agent_stream_events(result.stream_events()):
        yield event
