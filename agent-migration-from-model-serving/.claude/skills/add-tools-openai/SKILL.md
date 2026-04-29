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
from databricks_openai.agents import McpServer

genie_server = McpServer(
    url=f"{host}/api/2.0/mcp/genie/01234567-89ab-cdef",
    name="my genie space",
)

agent = Agent(
    name="my agent",
    model="databricks-claude-3-7-sonnet",
    mcp_servers=[genie_server],
)
```

**Step 2:** Grant access in `databricks.yml`:
```yaml
resources:
  apps:
    agent_migration:
      resources:
        - name: 'my_genie_space'
          genie_space:
            name: 'My Genie Space'
            space_id: '01234567-89ab-cdef'
            permission: 'CAN_RUN'
```

**Step 3:** Deploy with `databricks bundle deploy` (see **deploy** skill)

## Resource Type Examples

See the `examples/` directory for complete YAML snippets:

| File | Resource Type | When to Use |
|------|--------------|-------------|
| `uc-function.yaml` | Unity Catalog function | UC functions |
| `uc-connection.yaml` | UC connection | External MCP servers |
| `vector-search.yaml` | Vector search index | RAG applications |
| `sql-warehouse.yaml` | SQL warehouse | SQL execution |
| `serving-endpoint.yaml` | Model serving endpoint | Model inference |
| `genie-space.yaml` | Genie space | Natural language data |
| `lakebase-autoscaling.yaml` | Lakebase autoscaling postgres | Agent memory storage (autoscaling) |
| `experiment.yaml` | MLflow experiment | Tracing (already configured) |
| `app.yaml` | Databricks App (app-to-app) | Custom MCP servers hosted as Apps |
| `custom-mcp-server.md` | Custom MCP apps | Apps starting with `mcp-*` |

## Custom MCP Servers (Databricks Apps)

Declare the target app as an `app` resource in `databricks.yml` — the bundle grants `CAN_USE` on deploy. Requires Databricks CLI **v0.298.0+**.

```yaml
resources:
  apps:
    agent_migration:
      resources:
        - name: 'mcp_server'
          app:
            name: 'mcp-my-server'
            permission: CAN_USE
```

See `examples/custom-mcp-server.md` for the full flow (agent code + YAML + deploy).

## OBO (Per-User Authorization)

By default, MCP tool calls run as the app's service principal. To run them as the requesting user instead — so each user only sees data they personally have access to — declare the required scopes in `databricks.yml` and pass the user-scoped `WorkspaceClient` into each `McpServer`. **Do not configure scopes through the Databricks UI** — `user_api_scopes` in the DAB is the supported, deterministic path.

**Step 1:** declare scopes on the app resource in `databricks.yml`:

```yaml
resources:
  apps:
    agent_migration:
      user_api_scopes:
        - mcp.genie         # for Genie MCP routes
        - mcp.functions     # for UC function MCP routes
        # add one entry per tool category you call OBO
```

**Step 2:** in `agent_server/agent.py`, swap the SP `WorkspaceClient()` for `get_user_workspace_client()` *inside* the `@invoke`/`@stream` handler (not at module load — the user token is per-request):

```python
from agent_server.utils import get_user_workspace_client
from databricks_openai.agents import McpServer

@invoke()
async def invoke_handler(request):
    user_wc = get_user_workspace_client()  # per-request, never at startup
    genie_server = McpServer(
        url=f"{user_wc.config.host}/api/2.0/mcp/genie/<space-id>",
        name="genie",
        workspace_client=user_wc,
    )
    # ... agent setup using [genie_server]
```

**Scope reference** (from `apps/commons/src/conf/AppsCommonConf.scala`):

| Tool | OBO scope |
|---|---|
| Genie space (MCP) | `mcp.genie` |
| UC function (MCP) | `mcp.functions` |
| Vector Search (MCP) | `mcp.vectorsearch` |
| UC connection (MCP) | `mcp.external` |
| Custom MCP server (App-hosted or external) | `apps` |
| Databricks App | `apps` |
| Model serving endpoint (direct SDK) | `serving.serving-endpoints` |
| SQL warehouse (direct SDK) | `sql.warehouses` |

See: [Databricks Apps user authorization](https://docs.databricks.com/aws/en/generative-ai/agent-framework/agent-authentication?language=Declarative+Automation+Bundles#implement-user-authorization).

## MCP Error Handling

MCP tool calls can fail (network issues, permission errors, timeouts). The OpenAI Agents SDK catches tool errors by default and returns the error message to the LLM. To customize timeout behavior for MCP servers:

```python
mcp_server = McpServer(
    url=f"{host}/api/2.0/mcp/genie/{space_id}",
    name="genie",
    timeout=60.0,  # Increase timeout for slow tools like Genie (default: 20s)
)
```

For local function tools, see `create-tools` skill > `examples/local-python-tools.md` for `failure_error_function` patterns.

## Important Notes

- **MLflow experiment**: Already configured in template, no action needed
- **Multiple resources**: Add multiple entries under `resources:` list
- **Permission types vary**: Each resource type has specific permission values
- **Deploy after changes**: Run `databricks bundle deploy` after modifying `databricks.yml`
