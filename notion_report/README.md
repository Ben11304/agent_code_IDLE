# AgentUI Notion Report Tool

Local project-scoped reporting connector. Documentation checked against
`config.py`, `tool.py`, `mcp_server.py`, `run_mcp.py`, and AgentUI settings code
on 2026-09-07. No live Notion operation was performed for this documentation update.

- One verified System Hub can provision a project subtree on first use.
- Existing project bindings remain valid. Workers inherit the unique project
  destination; they do not each need a separate binding.
- Ambiguous/missing scope fails rather than selecting a workspace-root fallback.
- MCP tools do not accept model-supplied project/agent identity. The launcher owns scope.
- Credentials are supplied by environment (with a scoped local fallback), never by
  report payload, destination registry or browser form.

The client/MCP transport use the Python standard library. YAML report contracts
use PyYAML from the backend requirements.

## 1. Configure the server connection

Supply the connection token to the AgentUI server environment, conventionally
`NOTION_REPORT_TOKEN`; grant that connection access to the intended parent page.
Read/create/managed replacement need the corresponding service permissions. The
actual `Verify & bind` operation checks access rather than assuming it from a URL.
See the API references below for provider setup details.

Store credentials outside git, for example in the launcher environment or
`app/.env.local`. Do not put them in `destinations.json`, project YAML or prompts.

## 2. Bind a System Hub or existing project destination

Recommended application path: Dashboard → **Notion System Hub** → verify/bind the
parent URL and token environment name. An unbound project can then create/reuse one
direct child named `<project name> [<slug>]`, protected by an inter-process provisioning
lock. The canonical root owns the saved destination; all project agents inherit it.
This provisioning can create a page when first used; it is not merely a local lookup.

For an explicit existing project binding, the CLI remains available from repository root:

```bash
app/.venv/bin/python -m notion_report bind \
  --project my-project --agent BOSS \
  --parent-url 'https://www.notion.so/your-parent-page-link' \
  --token-env NOTION_REPORT_TOKEN
app/.venv/bin/python -m notion_report status --project my-project --agent BOSS --verify
```

`bind` verifies the token workspace and parent access before saving. The registry
is `notion_report/destinations.json` (gitignored), overridable by `NOTION_REPORT_CONFIG`.
It contains page/workspace identity and token variable names, not token values.

## 3. Preview and write

The generic report input example is [report.example.json](report.example.json):

```bash
app/.venv/bin/python -m notion_report create \
  --project my-project --agent BOSS \
  --report notion_report/report.example.json --dry-run
```

Removing `--dry-run` authorizes this CLI command to create the report/subpages under
the bound parent. Project-specific schemas may require a different payload. Application
MCP reporting passes that schema explicitly; the generic CLI example alone does not
load an arbitrary project's YAML contract.

For existing content, `append_notion_child_page` matches an exact direct-child title;
missing children require `create_if_missing=true`, multiple matches fail. Identical
content already at the tail can return unchanged. `sync_notion_managed_report` defaults
to dry-run and replaces only AgentUI marker-delimited blocks, preserving manual content.
This is separate from the immutable versioned-revision workflow in §5.

## 4. Scoped MCP surface

AgentUI configures the internal `agentui_notion_report` server. Manual scoped launch:

```bash
app/.venv/bin/python notion_report/run_mcp.py \
  --project my-project --agent BOSS \
  --config /absolute/path/to/destinations.json
```

`AGENTUI_PROJECT_SLUG` / `AGENTUI_AGENT_ID` environment are also supported; the
application passes canonical identity in argv to survive provider resume behavior.
Missing scope fails closed. Optional launcher metadata covers project name/root owner,
project directory and report schema. Never ask the model to infer those identifiers.

| Tool | Purpose |
|---|---|
| `notion_report_destination` | Inspect/verify scoped destination |
| `list_notion_project_tree` | Bounded inventory of bound project subtree |
| `read_notion_project_page` | Read an in-scope page with content hash |
| `get_current_report_schema` | Reload project schema/version/hash |
| `create_notion_report` | Validate and create a structured report |
| `append_notion_child_page` | Exact-child append, optional create, read-back |
| `sync_notion_managed_report` | Preview/apply managed content replacement |

“All Notion” in an AgentUI turn means the bound project's subtree, not the user's
whole workspace. Report success only after required read-back verification. AgentUI
blocks the raw/global Notion namespace in Claude/Codex scoped runtime paths; Codex
capability policy can also explicitly disable the internal server. Grok's adapter
does not currently receive this scoped MCP configuration, so the same connector
availability must not be promised for every provider.

## 5. Project-owned report form

Projects may version the report contract with their agents instead of embedding
one large template in every session. Add this to `.agentui/project.yaml`:

```yaml
notion_report:
  enabled: true
  mode: versioned
  schema_file: .agentui/report_schema.yaml
```

The schema defines the revision-title pattern, required main-page sections,
required subpages, each page's owner, and required section keys. AgentUI loads
the complete contract only when the user presses **Nạp Notion**. BOSS receives
the full contract and includes the corresponding owner slice when dispatching a
worker.

The same schema path and project root are passed to the destination-bound MCP
server. `create_notion_report` validates the assembled report before its first
Notion write and rejects a missing page, key, section, title, or body. The
custom writer then compiles that structured JSON to Notion enhanced Markdown;
the agent never receives a raw Notion write surface. After creation it reads
both rendered blocks and the Markdown view of the main page and every subpage
back before reporting success.

Sections may also contain native Notion tables:

```json
{
  "key": "results",
  "heading": "Verified results",
  "tables": [{
    "key": "experiment_results",
    "caption": "Experiment comparison",
    "columns": ["Arm", "Accuracy", "95% CI", "Boundary"],
    "rows": [["CONTROL", "73.8267%", "—", "validation-only"]]
  }]
}
```

The writer compiles the table to Notion Markdown, which creates a real `table`
block with `table_row` children. It rejects
duplicate columns, non-rectangular rows, more than 20 columns, more than 99 data
rows, and cells above 4,000 characters. Project schemas can attach
`required_tables` to a required section; the report must then use the exact
table key, column titles, and order. Optional `min_rows` (1–99) raises the
minimum data-row gate for complete ledgers.
Agents still provide structured `tables`; free-form Markdown pipe tables are
not accepted as a way to bypass the schema.

Sections may also contain a native Mermaid diagram and bold change highlights:

```json
{
  "key": "architecture_diagram",
  "heading": "Architecture diagram",
  "highlights": ["Replaced the prior ASCII sketch"],
  "diagrams": [{
    "key": "model_architecture",
    "format": "mermaid",
    "caption": "Recorded architecture",
    "source": "flowchart LR\nA[Inputs] --> B[Encoder]\nB -. unknown .-> C[Heads]"
  }]
}
```

Only `mermaid` is accepted. A project schema can declare `required_diagrams`
under a required section; missing keys and other formats fail before the first
Notion write. Read-back also verifies that the rendered block remains a code
block whose language is `mermaid`.

Changing the form is therefore a project change: edit the YAML file, increment
`schema_version`, then reload the project UI. Existing Notion revisions remain
immutable; the next accepted run creates a new successor revision.

### Reviewer-note authority

The **Nạp Notion** workflow recognizes two exact, system-owned instruction
headings in the latest source revision:

- `USER NOTE R-NNNN` authorizes content/work changes only. It can never change
  the report schema.
- `SCHEMA NOTE S-NNNN` is the sole authority to change the project report
  schema. All open schema notes in one source revision are applied as one
  transition and `schema_version` must increase by exactly one.

Formatting edits, deleted/moved blocks, ordinary prose, Notion comments, and a
`USER NOTE` never imply schema authority. The source revision remains immutable.
After an authorized edit the agent calls `get_current_report_schema`; this
reloads the YAML and returns the new version/hash/contract, which replaces the
form embedded when the button was pressed. `create_notion_report` independently
reloads and validates against the current schema before writing, so the new form
can take effect in the same agent turn.

The successor carries terminal receipts such as `USER NOTE R-0003 — DONE` and
`SCHEMA NOTE S-0001 — APPLIED`, including acceptance evidence and, for schema
changes, the old/new version and hash plus a structural diff. It must not copy
the exact open-note heading into the successor.

## Dashboard integration

The Dashboard already has **Notion System Hub** setup. Its API is
`GET/POST /api/notion-system-settings`. Per-agent status/binding uses
`GET/POST /api/projects/{slug}/agents/{agent_id}/notion-settings`; workers show
inherited destination status but cannot rebind the project resource.
The browser sends the parent URL and token **environment-variable name**, not
its raw secret. Verification records workspace/page identity before saving.

## Tests

```bash
python -m unittest discover -s notion_report/tests -v
```

The tests use a fake Notion client and make no network requests. They cover URL
parsing, secret-free persistence, correct parent nesting, dry-run behavior, and
blocking a changed workspace before any page is created.

## API references

- [Notion authorization](https://developers.notion.com/guides/get-started/authorization)
- [Create a page](https://developers.notion.com/reference/post-page)
- [Retrieve the token's bot user](https://developers.notion.com/reference/get-self)
- [Notion API versioning](https://developers.notion.com/reference/versioning)
- [Official Python MCP SDK](https://github.com/modelcontextprotocol/python-sdk)
