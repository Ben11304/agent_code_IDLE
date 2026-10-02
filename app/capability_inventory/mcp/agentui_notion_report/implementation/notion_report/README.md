# AgentUI Notion Report Tool

Local, destination-bound tool for creating structured Notion project reports.
It is intentionally stricter than the general Notion MCP connection:

- the agent cannot supply a parent page to `create_notion_report`;
- every `(project_slug, agent_id)` must be bound by the user first;
- binding records the workspace ID returned by the token;
- every write re-verifies the token workspace and parent-page access;
- there is no workspace-root fallback;
- access tokens stay in environment variables, never in the registry or report.

The Notion client and MCP entrypoint use only the Python standard library. A
project-owned YAML report form requires PyYAML, which is already part of the
AgentUI backend environment.

## 1. Create a Notion connection

Create an internal Notion connection in the intended workspace and enable read
and insert-content capabilities. Open the desired parent page, choose
`...` → `Connections` → `Add connection`, and select that connection. Notion
does not grant a new connection access to any page by default.

Export the token in the environment used to launch AgentUI/MCP:

```bash
export NOTION_GELSIGHT_TOKEN='secret_...'
```

Do not put the token in `destinations.json`, `project.yaml`, a prompt, or git.

## 2. Bind the BOSS destination

Run from the repository root:

```bash
python -m notion_report bind \
  --project gelsight \
  --agent BOSS \
  --parent-url 'https://www.notion.so/your-parent-page-link' \
  --token-env NOTION_GELSIGHT_TOKEN
```

`bind` calls `GET /v1/users/me` and `GET /v1/pages/{parent_page_id}` before it
writes the local registry. The saved record contains only the environment
variable name, verified workspace ID/name, and parent page ID/title.

Verify it again at any time:

```bash
python -m notion_report status --project gelsight --agent BOSS --verify
```

The runtime registry is `notion_report/destinations.json` and is gitignored.
Set `NOTION_REPORT_CONFIG` to move it elsewhere.

## 3. Create or preview a report

Use [`report.example.json`](report.example.json) as the input schema:

```bash
python -m notion_report create \
  --project gelsight \
  --agent BOSS \
  --report notion_report/report.example.json \
  --dry-run
```

Remove `--dry-run` to create the parent report and its subpages. The report is
always created under the bound parent; subpages are always created under the
new report page.

## 4. Expose it as an MCP tool

Register this stdio command in the MCP client:

```bash
python \
  /absolute/path/to/agent_code_IDLE/notion_report/run_mcp.py
```

The server exposes:

- `notion_report_destination` — show or live-verify the bound destination;
- `create_notion_report` — create a report without a parent argument.

For per-agent isolation, launch the MCP process with:

```bash
AGENTUI_PROJECT_SLUG=gelsight
AGENTUI_AGENT_ID=BOSS
NOTION_REPORT_CONFIG=/absolute/path/to/destinations.json
```

When these scope variables are present, attempts to pass another project or
agent ID are rejected. The AgentUI adapter should set them when it launches the
CLI. Direct write-capable tools from the general Notion MCP should then be
disabled for that agent; otherwise the agent could bypass this destination gate.

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

## Dashboard integration contract

The future BOSS settings panel only needs to collect:

- parent page URL;
- token environment-variable name (or a preconfigured connection selector).

Its `Verify & bind` endpoint should call
`NotionReportTool.bind_destination(project_slug, agent_id, parent_url, token_env)`.
Do not let the browser send or store a raw token. After binding, display the
returned workspace name, parent title, and verified status.

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
