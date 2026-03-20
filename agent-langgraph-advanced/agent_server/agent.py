import logging
import os
from datetime import datetime
from typing import Any, AsyncGenerator, Optional, Sequence, TypedDict

import litellm
import mlflow
from databricks.sdk import WorkspaceClient
from databricks_langchain import (
    AsyncCheckpointSaver,
    AsyncDatabricksStore,
    ChatDatabricks,
    DatabricksMCPServer,
    DatabricksMultiServerMCPClient,
)
from fastapi import HTTPException
from langchain.agents import create_agent
from langchain_core.messages import AnyMessage
from langchain_core.tools import tool
from langgraph.graph.message import add_messages
from langgraph.store.base import BaseStore
from mlflow.genai.agent_server import invoke, stream
from mlflow.types.responses import (
    ResponsesAgentRequest,
    ResponsesAgentResponse,
    ResponsesAgentStreamEvent,
    to_chat_completions_input,
)
from typing_extensions import Annotated

from agent_server.utils import (
    _get_lakebase_access_error_message,
    _get_or_create_thread_id,
    get_databricks_host_from_env,
    process_agent_astream_events,
)
from agent_server.utils_memory import (
    get_user_id,
    memory_tools,
    resolve_lakebase_instance_name,
)

logger = logging.getLogger(__name__)
mlflow.langchain.autolog()
logging.getLogger("mlflow.utils.autologging_utils").setLevel(logging.ERROR)
litellm.suppress_debug_info = True
sp_workspace_client = WorkspaceClient()


@tool
def get_current_time() -> str:
    """Get the current date and time."""
    return datetime.now().isoformat()


############################################
# Configuration
############################################
LLM_ENDPOINT_NAME = "databricks-claude-sonnet-4-5"
_LAKEBASE_INSTANCE_NAME_RAW = os.getenv("LAKEBASE_INSTANCE_NAME") or None
EMBEDDING_ENDPOINT = "databricks-gte-large-en"
EMBEDDING_DIMS = 1024
LAKEBASE_AUTOSCALING_PROJECT = os.getenv("LAKEBASE_AUTOSCALING_PROJECT") or None
LAKEBASE_AUTOSCALING_BRANCH = os.getenv("LAKEBASE_AUTOSCALING_BRANCH") or None

_has_autoscaling = LAKEBASE_AUTOSCALING_PROJECT and LAKEBASE_AUTOSCALING_BRANCH
if not _LAKEBASE_INSTANCE_NAME_RAW and not _has_autoscaling:
    raise ValueError(
        "Lakebase configuration is required but not set. "
        "Please set one of the following in your environment:\n"
        "  Option 1 (provisioned): LAKEBASE_INSTANCE_NAME=<your-instance-name>\n"
        "  Option 2 (autoscaling): LAKEBASE_AUTOSCALING_PROJECT=<project> and LAKEBASE_AUTOSCALING_BRANCH=<branch>\n"
    )

# Resolve hostname to instance name if needed (if given hostname of lakebase instead of name)
LAKEBASE_INSTANCE_NAME = resolve_lakebase_instance_name(_LAKEBASE_INSTANCE_NAME_RAW) if _LAKEBASE_INSTANCE_NAME_RAW else None

SYSTEM_PROMPT = """You are a helpful assistant. Use the available tools to answer questions.

You have access to memory tools that allow you to remember information about users:
- Use get_user_memory to search for previously saved information about the user
- Use save_user_memory to remember important facts, preferences, or details the user shares
- Use delete_user_memory to forget specific information when asked

Always check for relevant memories at the start of a conversation to provide personalized responses.

## When to save memories

**Always save** when the user explicitly asks you to remember something. Trigger phrases include:
"remember that…", "store this", "add to memory", "note that…", "from now on…"

**Proactively save** when the user shares information that is likely to remain true for months or years \
and would meaningfully improve future responses. This includes:
- Preferences (e.g., language, framework, formatting style)
- Role, responsibilities, or expertise
- Ongoing projects or long-term goals
- Recurring constraints (e.g., accessibility needs, dietary restrictions)

## When NOT to save memories

- Temporary or short-lived facts (e.g., "I'm tired today")
- Trivial or one-off details (e.g., what they ate for lunch, a single troubleshooting step)
- Highly sensitive personal information (health conditions, political affiliation, sexual orientation, \
religion, criminal history) — unless the user explicitly asks you to store it
- Information that could feel intrusive or overly personal to store"""


class StatefulAgentState(TypedDict, total=False):
    messages: Annotated[Sequence[AnyMessage], add_messages]
    custom_inputs: dict[str, Any]
    custom_outputs: dict[str, Any]


def init_mcp_client(workspace_client: WorkspaceClient) -> DatabricksMultiServerMCPClient:
    host_name = get_databricks_host_from_env()
    return DatabricksMultiServerMCPClient(
        [
            DatabricksMCPServer(
                name="system-ai",
                url=f"{host_name}/api/2.0/mcp/functions/system/ai",
                workspace_client=workspace_client,
            ),
        ]
    )


async def init_agent(
    store: BaseStore,
    workspace_client: Optional[WorkspaceClient] = None,
    checkpointer: Optional[Any] = None,
):
    tools = [get_current_time] + memory_tools()
    # To use MCP server tools instead, uncomment the below lines:
    # mcp_client = init_mcp_client(workspace_client or sp_workspace_client)
    # try:
    #     tools.extend(await mcp_client.get_tools())
    # except Exception:
    #     logger.warning("Failed to fetch MCP tools. Continuing without MCP tools.", exc_info=True)

    model = ChatDatabricks(endpoint=LLM_ENDPOINT_NAME)

    return create_agent(
        model=model,
        tools=tools,
        system_prompt=SYSTEM_PROMPT,
        checkpointer=checkpointer,
        store=store,
        state_schema=StatefulAgentState,
    )


@invoke()
async def invoke_handler(request: ResponsesAgentRequest) -> ResponsesAgentResponse:
    thread_id = _get_or_create_thread_id(request)
    request.custom_inputs = dict(request.custom_inputs or {})
    request.custom_inputs["thread_id"] = thread_id

    outputs = [
        event.item
        async for event in stream_handler(request)
        if event.type == "response.output_item.done"
    ]

    user_id = get_user_id(request)
    custom_outputs: dict[str, Any] = {"thread_id": thread_id}
    if user_id:
        custom_outputs["user_id"] = user_id
    return ResponsesAgentResponse(output=outputs, custom_outputs=custom_outputs)


@stream()
async def stream_handler(
    request: ResponsesAgentRequest,
) -> AsyncGenerator[ResponsesAgentStreamEvent, None]:
    thread_id = _get_or_create_thread_id(request)
    mlflow.update_current_trace(metadata={"mlflow.trace.session": thread_id})

    user_id = get_user_id(request)
    if not user_id:
        logger.warning("No user_id provided - memory features will not be available")

    config: dict[str, Any] = {"configurable": {"thread_id": thread_id}}
    if user_id:
        config["configurable"]["user_id"] = user_id

    input_state: dict[str, Any] = {
        "messages": to_chat_completions_input([i.model_dump() for i in request.input]),
        "custom_inputs": dict(request.custom_inputs or {}),
    }

    try:
        async with AsyncCheckpointSaver(
            instance_name=LAKEBASE_INSTANCE_NAME,
            project=LAKEBASE_AUTOSCALING_PROJECT,
            branch=LAKEBASE_AUTOSCALING_BRANCH,
        ) as checkpointer, AsyncDatabricksStore(
            instance_name=LAKEBASE_INSTANCE_NAME,
            project=LAKEBASE_AUTOSCALING_PROJECT,
            branch=LAKEBASE_AUTOSCALING_BRANCH,
            embedding_endpoint=EMBEDDING_ENDPOINT,
            embedding_dims=EMBEDDING_DIMS,
        ) as store:
            await checkpointer.setup()
            await store.setup()

            config["configurable"]["store"] = store

            # By default, uses service principal credentials.
            # For on-behalf-of user authentication, pass get_user_workspace_client() to init_agent.
            agent = await init_agent(store=store, checkpointer=checkpointer)

            async for event in process_agent_astream_events(
                agent.astream(
                    input_state,
                    config,
                    stream_mode=["updates", "messages"],
                )
            ):
                yield event
    except Exception as e:
        error_msg = str(e).lower()
        # Check for Lakebase access/connection errors
        if any(keyword in error_msg for keyword in ["lakebase", "pg_hba", "postgres", "database instance"]):
            logger.error(f"Lakebase access error: {e}")
            lakebase_desc = LAKEBASE_INSTANCE_NAME or f"{LAKEBASE_AUTOSCALING_PROJECT}/{LAKEBASE_AUTOSCALING_BRANCH}"
            raise HTTPException(
                status_code=503, detail=_get_lakebase_access_error_message(lakebase_desc)
            ) from e
        raise
