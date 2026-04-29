---
name: add-tools
description: "Add tools to your agent and grant required permissions in databricks.yml. Use when: (1) Adding MCP servers, Genie spaces, vector search, or UC functions to agent, (2) Permission errors at runtime, (3) User says 'add tool', 'connect to', 'grant permission', (4) Configuring databricks.yml resources."
---

# Add Tools & Grant Permissions

> **Profile reminder:** All `databricks` CLI commands must include the profile from `.env`: `databricks <command> --profile <profile>`

> Don't have the resource yet? See **create-tools** skill first.

**After adding any MCP server to your agent, you MUST grant the app access in `databricks.yml`.**

Without this, you'll get permission errors when the agent tries to use the resource.

## Workflow

**Step 1:** Add MCP server in `agent_server/agent.py`:
```python
from databricks_langchain import DatabricksMCPServer, DatabricksMultiServerMCPClient

genie_server = DatabricksMCPServer(
    url=f"{host}/api/2.0/mcp/genie/01234567-89ab-cdef",
    name="my genie space",
)

mcp_client = DatabricksMultiServerMCPClient([genie_server])
tools = await mcp_client.get_tools()
```

**Step 2:** Grant access in `databricks.yml`:
```yaml
resources:
  apps:
    agent_langgraph:
      resources:
        - name: 'my_genie_space'
          genie_space:
            name: 'My Genie Space'
            space_id: '01234567-89ab-cdef'
            permission: 'CAN_RUN'
```

**Step 3:** Deploy and run:
```bash
databricks bundle deploy
databricks bundle run agent_langgraph  # Required to start app with new code!
```

See **deploy** skill for more details.

## Resource Type Examples

See the `examples/` directory for complete YAML snippets:

| File | Resource Type | When to Use |
|------|--------------|-------------|
| `uc-function.yaml` | Unity Catalog function | UC functions via MCP |
| `uc-connection.yaml` | UC connection | External MCP servers |
| `vector-search.yaml` | Vector search index | RAG applications |
| `sql-warehouse.yaml` | SQL warehouse | SQL execution |
| `serving-endpoint.yaml` | Model serving endpoint | Model inference |
| `genie-space.yaml` | Genie space | Natural language data |
| `lakebase.yaml` | Lakebase database | Agent memory storage (provisioned) |
| `lakebase-autoscaling.yaml` | Lakebase autoscaling postgres | Agent memory storage (autoscaling) |
| `experiment.yaml` | MLflow experiment | Tracing (already configured) |
| `app.yaml` | Databricks App (app-to-app) | Custom MCP servers hosted as Apps |
| `custom-mcp-server.md` | Custom MCP apps | Apps starting with `mcp-*` |

## Custom MCP Servers (Databricks Apps)

Declare the target app as an `app` resource in `databricks.yml` — the bundle grants `CAN_USE` on deploy. Requires Databricks CLI **v0.298.0+**.

```yaml
resources:
  apps:
    agent_langgraph:
      resources:
        - name: 'mcp_server'
          app:
            name: 'mcp-my-server'
            permission: CAN_USE
```

See `examples/custom-mcp-server.md` for the full flow (agent code + YAML + deploy).

## value_from Pattern

**IMPORTANT**: Make sure all `value_from` references in `databricks.yml` `config.env` reference an existing key in the `databricks.yml` `resources` list.
Some resources need environment variables in your app. Use `value_from` in `databricks.yml` `config.env` to reference resources defined in `databricks.yml`:

```yaml
# In databricks.yml, under apps.<app>.config.env:
env:
  - name: MLFLOW_EXPERIMENT_ID
    value_from: "experiment"        # References resources.apps.<app>.resources[name='experiment']
  - name: LAKEBASE_INSTANCE_NAME
    value_from: "database"   # References resources.apps.<app>.resources[name='database']
```

**Critical:** Every `value_from` value must match a `name` field in `databricks.yml` resources.

## OBO (Per-User Authorization)

By default, tool calls run as the app's service principal. To run them as the requesting user — so each user only sees data they personally have access to — declare the required scopes in `databricks.yml` and pass the user-scoped `WorkspaceClient` into each MCP server (or direct SDK client). **Do not configure scopes through the Databricks UI** — `user_api_scopes` in the DAB is the supported, deterministic path.

**Step 1:** declare scopes on the app resource in `databricks.yml`:

```yaml
resources:
  apps:
    {{BUNDLE_NAME}}:
      user_api_scopes:
        - mcp.genie                         # Genie via MCP
        - vectorsearch.vector-search-indexes # direct vector-search SDK
        # add one entry per tool category you call OBO
```

**Step 2:** in `agent_server/agent.py`, swap the SP `WorkspaceClient()` for `get_user_workspace_client()` *inside* the `@invoke`/`@stream` handler (not at module load — the user token is per-request):

```python
from agent_server.utils import get_user_workspace_client
from databricks_langchain import DatabricksMCPServer, DatabricksMultiServerMCPClient

@invoke()
async def invoke_handler(request):
    user_wc = get_user_workspace_client()  # per-request, never at startup
    genie_server = DatabricksMCPServer(
        url=f"{user_wc.config.host}/api/2.0/mcp/genie/<space-id>",
        name="genie",
        workspace_client=user_wc,
    )
    mcp_client = DatabricksMultiServerMCPClient([genie_server])
    tools = await mcp_client.get_tools()
    # ... build agent with `tools`
```

**Scope reference** (from `apps/commons/src/conf/AppsCommonConf.scala`). Pick the row for the path you actually use — MCP routes use `mcp.*`; direct SDK calls use the resource-specific scope:

| Tool | MCP scope | Direct-SDK scope |
|---|---|---|
| Genie space | `mcp.genie` | `dashboards.genie` |
| UC function | `mcp.functions` | n/a |
| Vector Search | `mcp.vectorsearch` | `vectorsearch.vector-search-indexes` |
| UC connection | `mcp.external` | `catalog.connections` |
| Custom MCP server (App-hosted or external) | `apps` | n/a |
| Databricks App | `apps` | `apps` |
| Model serving endpoint | n/a | `serving.serving-endpoints` |
| SQL warehouse | n/a | `sql.warehouses` |

See: [Databricks Apps user authorization](https://docs.databricks.com/aws/en/generative-ai/agent-framework/agent-authentication?language=Declarative+Automation+Bundles#implement-user-authorization).

## MCP Error Handling

MCP tool calls can fail (network issues, permission errors, timeouts). Use `handle_tool_error` on MCP servers to catch errors and return them to the LLM instead of crashing the agent:

```python
DatabricksMCPServer(
    name="genie",
    url=f"{host}/api/2.0/mcp/genie/{space_id}",
    handle_tool_error=True,   # Return error messages to LLM instead of raising
    timeout=60.0,             # Increase timeout for slow tools like Genie
)
```

For local function tools defined with `@tool`, see `create-tools` skill > `examples/local-python-tools.md` for the `ToolException` + `handle_tool_error` pattern.

## Important Notes

- **MLflow experiment**: Already configured in template, no action needed
- **Multiple resources**: Add multiple entries under `resources:` list
- **Permission types vary**: Each resource type has specific permission values
- **Deploy + Run after changes**: Run both `databricks bundle deploy` AND `databricks bundle run {{BUNDLE_NAME}}`
- **value_from matching**: Ensure `config.env` `value_from` values match `databricks.yml` resource `name` values
