import os
import uuid
from typing import AsyncGenerator, Optional

import mlflow
from databricks.sdk import WorkspaceClient
from databricks_langchain import (
    AsyncDatabricksStore,
    ChatDatabricks,
    DatabricksMCPServer,
    DatabricksMultiServerMCPClient,
)
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool
from langgraph.prebuilt import create_react_agent
from mlflow.genai.agent_server import invoke, stream
from mlflow.types.responses import (
    ResponsesAgentRequest,
    ResponsesAgentResponse,
    ResponsesAgentStreamEvent,
    to_chat_completions_input,
)

from agent_server.utils import (
    get_databricks_host_from_env,
    process_agent_astream_events,
)

mlflow.langchain.autolog()
sp_workspace_client = WorkspaceClient()

# Environment configuration
LAKEBASE_INSTANCE_NAME = os.getenv("LAKEBASE_INSTANCE_NAME")
EMBEDDING_ENDPOINT = os.getenv("EMBEDDING_ENDPOINT", "databricks-gte-large-en")
EMBEDDING_DIMS = int(os.getenv("EMBEDDING_DIMS", "1024"))

AGENT_INSTRUCTIONS = """You are a helpful customer support assistant. Your role is to help customer support representatives be more efficient by:

1. **Always remembering user preferences** - Save and recall all preferences that users express
2. **Answering questions about customer reviews** - Use the review tools to search, filter, and analyze customer feedback
3. **Providing product documentation and policies** - Use the policy tools to find relevant documentation


## Available Tools

### Customer Reviews
- `query_reviews` - Search reviews with flexible regex filtering on product names
- `get_recent_reviews` - Get reviews from the past N days
- `list_reviews` - Browse all reviews ordered by most recent

### Documentation & Policies
- `read_latest_policies` - Read the latest policy documents
- `get_latest_policy_docs` - Get policy documentation

### User Memory
- `save_user_memory` - Save user preferences and important details for future reference
- `get_user_memory` - Recall saved information about the user

## Guidelines

1. When users express preferences (e.g., "I prefer detailed answers", "Call me Alex", "I work with the shipping team", "my customer wants to buy an item), proactively save these using `save_user_memory`
2. At the start of conversations, check for relevant saved memories about the user
3. When searching reviews, try to understand what the user is really looking for and use the most appropriate tool
4. Always cite specific reviews or policy sections when providing information
5. Be concise but thorough in your responses
"""


@tool
async def get_user_memory(query: str, config: RunnableConfig) -> str:
    """Search for relevant information about the user from long-term memory.
    Use this to recall preferences, past interactions, or other saved information.

    Args:
        query: What to search for in the user's saved memories
    """
    user_id = config.get("configurable", {}).get("user_id")
    store = config.get("configurable", {}).get("store")
    if not user_id or not store:
        return "Memory not available - no user context"

    namespace = ("user_memories", user_id.replace(".", "-").replace("@", "-"))
    results = await store.asearch(namespace, query=query, limit=5)
    if not results:
        return "No memories found for this query"
    return "\n".join([f"- {r.value}" for r in results])


@tool
async def save_user_memory(memory_key: str, memory_data: str, config: RunnableConfig) -> str:
    """Save information about the user to long-term memory.
    Use this to remember user preferences, important details, or other
    information that should persist across conversations.

    Args:
        memory_key: A short descriptive key (e.g., "preferred_name", "team", "communication_style")
        memory_data: The information to save
    """
    user_id = config.get("configurable", {}).get("user_id")
    store = config.get("configurable", {}).get("store")
    if not user_id or not store:
        return "Memory not available - no user context"

    namespace = ("user_memories", user_id.replace(".", "-").replace("@", "-"))
    await store.aput(namespace, memory_key, {"value": memory_data})
    return f"Saved memory: {memory_key}"


@tool
async def delete_user_memory(memory_key: str, config: RunnableConfig) -> str:
    """Delete a specific memory from the user's long-term memory."""
    user_id = config.get("configurable", {}).get("user_id")
    if not user_id:
        return "Cannot delete memory - no user_id provided."

    store = config.get("configurable", {}).get("store")
    if not store:
        return "Cannot delete memory - store not configured."

    namespace = ("user_memories", user_id.replace(".", "-"))
    await store.adelete(namespace, memory_key)
    return f"Successfully deleted memory '{memory_key}' for user."


def init_mcp_client(workspace_client: WorkspaceClient) -> DatabricksMultiServerMCPClient:
    host_name = get_databricks_host_from_env()
    return DatabricksMultiServerMCPClient(
        [
            # Customer support UC functions (reviews, policies)
            DatabricksMCPServer(
                name="customer-support",
                url=f"{host_name}/api/2.0/mcp/functions/agent_cuj/customer_support",
            ),
        ]
    )


def _get_user_id(request: ResponsesAgentRequest) -> str:
    """Extract user_id from request context or custom inputs."""
    custom_inputs = dict(request.custom_inputs or {})
    if "user_id" in custom_inputs and custom_inputs["user_id"]:
        return str(custom_inputs["user_id"])
    if request.context and getattr(request.context, "user_id", None):
        return str(request.context.user_id)
    return "default-user"


def _get_or_create_thread_id(request: ResponsesAgentRequest) -> str:
    """Extract or create thread_id for conversation tracking."""
    custom_inputs = dict(request.custom_inputs or {})
    if "thread_id" in custom_inputs and custom_inputs["thread_id"]:
        return str(custom_inputs["thread_id"])
    if request.context and getattr(request.context, "conversation_id", None):
        return str(request.context.conversation_id)
    return str(uuid.uuid4())


@invoke()
async def non_streaming(request: ResponsesAgentRequest) -> ResponsesAgentResponse:
    outputs = [
        event.item
        async for event in streaming(request)
        if event.type == "response.output_item.done"
    ]
    return ResponsesAgentResponse(output=outputs)


@stream()
async def streaming(
    request: ResponsesAgentRequest,
) -> AsyncGenerator[ResponsesAgentStreamEvent, None]:
    # Get user context
    user_id = _get_user_id(request)
    thread_id = _get_or_create_thread_id(request)

    # Get MCP tools (customer support functions)
    mcp_client = init_mcp_client(sp_workspace_client)
    mcp_tools = await mcp_client.get_tools()

    # Memory tools
    memory_tools = [get_user_memory, save_user_memory, delete_user_memory]

    # Combine all tools
    all_tools = mcp_tools + memory_tools

    # Initialize model
    model = ChatDatabricks(endpoint="databricks-claude-sonnet-4")

    # Use AsyncDatabricksStore for long-term memory
    async with AsyncDatabricksStore(
        instance_name=LAKEBASE_INSTANCE_NAME,
        embedding_endpoint=EMBEDDING_ENDPOINT,
        embedding_dims=EMBEDDING_DIMS,
    ) as store:
        # Create agent with tools and instructions
        agent = create_react_agent(
            model=model,
            tools=all_tools,
            prompt=AGENT_INSTRUCTIONS,
        )

        # Prepare input
        messages = {"messages": to_chat_completions_input([i.model_dump() for i in request.input])}

        # Configure with user context for memory tools
        config = {
            "configurable": {
                "user_id": user_id,
                "thread_id": thread_id,
                "store": store,
            }
        }

        # Stream agent responses
        async for event in process_agent_astream_events(
            agent.astream(input=messages, config=config, stream_mode=["updates", "messages"])
        ):
            yield event
