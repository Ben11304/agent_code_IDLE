# OpenConstruction MCP

Local-first MCP server for OpenConstruction catalog discovery, dataset context,
provider-aware acquisition, and safe dataset downloads.

The first release is a local stdio MCP server. By default it reads the public catalog snapshot under [`Ben11304/OC-clone/open-construction-data`](https://github.com/Ben11304/OC-clone/tree/main/open-construction-data), normalizes the records, and exposes them to MCP-compatible assistants. This keeps the MCP install independent from the upstream OpenConstruction deployment.

Override `OPENCONSTRUCTION_DATA_BASE_URL` to use another compatible catalog endpoint. For example, set it to `https://www.openconstruction.org/data` to follow the deployed OpenConstruction site instead.

The remote entry point adds OAuth 2.1 authorization with PKCE, protected-resource discovery, dynamic client registration, refresh-token rotation, and server-side connected accounts for GitHub, Hugging Face, and Baidu Netdisk.

## Install With Your Agent

Copy this prompt into an MCP-compatible coding agent:

```text
Install and configure the OpenConstruction MCP for this agent from https://github.com/Ben11304/OC-mcp.
```

## Manual Install

Install the published Python library and CLI:

```bash
python -m pip install openconstruction
oc --help
```

To work from a source checkout instead:

```bash
git clone https://github.com/Ben11304/OC-mcp.git
cd OC-mcp
uv sync --python 3.12 --frozen
```

Register it with Codex, replacing the project path with the absolute path to
your checkout:

```bash
codex mcp add openconstruction -- uv --directory /absolute/path/to/OC-mcp run --frozen openconstruction-mcp
```

## Connect To Claude Desktop

Add this server to your Claude Desktop MCP configuration:

```json
{
  "mcpServers": {
    "openconstruction": {
      "command": "uv",
      "args": ["--directory", "/absolute/path/to/OC-mcp", "run", "--frozen", "openconstruction-mcp"]
    }
  }
}
```

Restart Claude Desktop after saving the configuration.

## Remote MCP with OpenConstruction login

Run the website and remote MCP/API on one origin during development:

```bash
cp .env.example .env
# Fill SUPABASE_URL, SUPABASE_ANON_KEY, and OC_TOKEN_ENCRYPTION_KEY.
set -a && source .env && set +a
uv run openconstruction-remote
```

The remote MCP endpoint is:

```text
http://127.0.0.1:8000/mcp
```

Compatible MCP clients discover OAuth through:

- `/.well-known/oauth-protected-resource/mcp`
- `/.well-known/oauth-authorization-server`
- `/register`, `/authorize`, `/token`, and `/revoke`

The client opens the OpenConstruction authorization page. The user signs in with the existing Supabase-backed OC account and approves the MCP client. Authorization codes are single-use, PKCE S256 is mandatory, access tokens last one hour, and rotating refresh tokens last up to 30 days.

HTTPS is required outside loopback development.

## Connected Accounts

Connected Accounts is implemented but deferred and disabled by default. Set `OC_CONNECTED_ACCOUNTS_ENABLED=true` when the provider applications, production secret storage, and privacy review are ready. Signed-in users will then manage provider access under **Workspace → Connections**. Public resources do not require a connected account.

Create one OAuth application per provider and register these callbacks, replacing the host with `OC_PUBLIC_URL`:

```text
/api/connections/github/callback
/api/connections/huggingface/callback
/api/connections/baidu/callback
```

Set the corresponding `OC_GITHUB_*`, `OC_HF_*`, and `OC_BAIDU_*` variables from `.env.example`. Provider access and refresh tokens are encrypted with `OC_TOKEN_ENCRYPTION_KEY`; API responses expose only connection status and public account metadata. Keep that key and all provider client secrets in the server's secret manager, never in the website bundle.

Provider references:

- [GitHub OAuth Apps](https://docs.github.com/en/apps/oauth-apps/building-oauth-apps/authorizing-oauth-apps)
- [Hugging Face OAuth](https://huggingface.co/docs/hub/en/oauth)
- [Baidu OAuth](https://openauth.baidu.com/doc/doc.html)

## Metadata Sources

- `/data/datasets.json`
- `/data/models.json`
- `/data/use-cases.json`
- `/data/oer.json`
- `/data/tools.json`
- `/data/guides.json`
- `/data/contributors.json`
- `/data/benchmark-results.json`
- `/data/task-vocabulary.json`

## MCP Tools

- `search_resources`
- `get_resource`
- `compare_resources`
- `get_catalog_stats`
- `ask_openconstruction`
- `find_datasets`
- `run_dataset_discovery`
- `find_models`
- `explain_schema`
- `analyze_catalog_gaps`
- `prepare_benchmark_submission`
- `validate_metadata_record`
- `list_skills`
- `get_skill`
- `get_dataset_download_plan`
- `get_dataset_paper_plan`
- `prepare_dataset_for_research` (local stdio only)
- `get_research_preparation_status` (local stdio only)
- `download_dataset` (local stdio only)
- `get_download_status` (local stdio only)
- `cancel_download` (local stdio only)

## Dataset Downloads

The `openconstruction` package provides a local `oc` command. A normal CLI download
installs only the dataset; add `--paper` to resolve and download the related
paper into the same package:

```bash
oc download TunGPR --accept-license
oc download TunGPR --paper --accept-license
```

Use `--library-dir` to select the dataset library and `--destination` to choose
the dataset-directory name inside it:

```bash
oc download TunGPR --paper --accept-license \
  --library-dir /data/openconstruction \
  --destination TunGPR
```

The CLI prints the dataset license before starting. `--accept-license` is an
explicit confirmation that the user reviewed and accepted that license and the
source terms; a matching interrupted checkpoint remembers the earlier
acceptance and can resume without it. When `--paper` is present and the OC paper
catalog has an available artifact, the original PDF is stored at
`<dataset>/papers/paper.pdf`, included in progress totals when its size is known,
checksum-verified, and recorded in `.openconstruction-manifest.json`. If no
paper is registered, the CLI reports that clearly and still completes the
dataset installation.

For the simplest researcher-facing workflow, ask the agent to prepare a dataset
for research. The MCP prefers `prepare_dataset_for_research`, returns one
plain-language license review, and waits for explicit acceptance before it
starts. The agent then polls `get_research_preparation_status` and presents one
progress stream instead of exposing the underlying download and bundle steps.

```text
Prepare TunGPR for my research.
```

The completed package contains the dataset, its original related PDF when
available, `.openconstruction-manifest.json`, and a small `research/` directory
with `bundle.json` plus a human-readable `README.md`. OpenConstruction preserves
the PDF byte-for-byte: it does not convert it to Markdown, extract its content,
chunk it, or create a document search index. Completed bundle metadata is stored
on disk, so `get_research_preparation_status` can rediscover a ready package
after the MCP restarts.

Reuse and resume are automatic. A normal request such as `Prepare TunGPR for my
research` follows this order:

1. Return the existing Research Bundle immediately when its dataset source,
   selected paper artifact, and dataset-license fingerprint still match.
2. Resume a matching interrupted checkpoint without asking the user to accept
   the same license again.
3. Start a new download only when no matching local package or checkpoint exists.

Persistent job state is kept under
`OC_DATASETS_DIR/.openconstruction-state/`. HTTP downloads retain an adjacent
`.part` file and use `Range` plus `If-Range` with the saved ETag or
Last-Modified value when the host supports it. A host that ignores range
requests restarts only that incomplete file, not already verified files in the
package. Figshare resumes each file through the same HTTP mechanism; Hugging
Face reuses its local snapshot cache; an interrupted Git clone reuses a valid
matching checkout and fetches the missing revision when necessary.

Provider-backed datasets can declare a stable source identity separately from
the adapter and URL used to transfer their files. If a provider adapter changes,
OpenConstruction automatically resets an older failed checkpoint only when the
target is provably empty (zero downloaded bytes and no files or symbolic links).
Any non-empty partial download is preserved and reported as a conflict instead
of being deleted or overwritten. Zenodo records use their immutable record ID
for this identity, so switching from the unsupported record-archive endpoint to
per-file downloads does not require a renamed destination.

OpenConstruction verifies the size and SHA-256 of every completed HTTP file
before reusing it. On POSIX systems it also holds a per-destination lock so two
local MCP processes do not download the same package concurrently. If the
catalog route, paper route, or license changes, the source fingerprint changes
and the MCP returns `source_changed` rather than overwriting the existing
directory. Git and Hugging Face sources that name a mutable branch still cannot
predict a new upstream commit until the catalog pins or updates that revision.

The lower-level download tools remain available for advanced control and
diagnostics:

OpenConstruction maintains a local dataset library similar to other dataset
hubs. Its default layout is:

```text
~/.openconstruction/
├── datasets/
│   ├── TunGPR/
│   ├── VizArQA/
│   └── .openconstruction-state/
└── ...
```

Set `OC_HOME` to move all future OC state, or `OC_DATASETS_DIR` to configure
only the default dataset library. `OC_DOWNLOAD_ROOT` remains supported as a
legacy fallback. A user can also choose a library for one request with the
`library_dir` argument; `destination` remains only the safe dataset-directory
name inside that library.

Before downloading, call `check_dataset_installation` with the same dataset,
paper choice, destination, and optional `library_dir`. It returns
`not_installed`, `installed`, `partial`, `unmanaged_directory`, or
`source_conflict`. Starting a matching installed package returns
`already_installed` immediately. A matching partial checkpoint resumes, while
an unmanaged or different-source directory is never overwritten.

```json
{
  "dataset_id": "TunGPR",
  "library_dir": "/data/openconstruction",
  "include_papers": true
}
```

OpenConstruction uses the same two acquisition routes as the website:

- `distribution` records resolve to a direct local download.
- `programmatic_access` records resolve to a provider adapter or structured CLI guidance.

Always call `get_dataset_download_plan` first. It is read-only and reports the
provider, method, license, authentication requirement, estimated size, and
whether the local MCP can execute the route. `download_dataset` requires
`accept_license: true` and starts a background job. Poll the returned
`download_id` with `get_download_status`; completed downloads include
`.openconstruction-manifest.json` in the dataset directory.

Every download status includes `progress_percent`, `progress_bar`, average
`speed_bytes_per_second`, `eta_seconds`, and a ready-to-display `progress_text`.
MCP instructions ask compatible agents to poll at most once every two seconds
and show that text until the job reaches a terminal status. Downloads whose
provider does not expose a total size return an indeterminate bar and the bytes
received instead of an unreliable percentage.

When catalog metadata omits a file size, HTTP providers make a best-effort
`HEAD` request and then a one-byte range request when needed. Figshare uses its
file API metadata, and Hugging Face requests repository file metadata before
starting a snapshot. If every required artifact size is known, OC reports a
determinate total, percentage, progress bar, speed, and ETA; otherwise it keeps
the progress explicitly indeterminate instead of inventing a total.

Related papers are included by default. `get_dataset_download_plan` returns a
`paper_plan`, and `download_dataset` treats an omitted `include_papers` argument
as `true`. When the OC paper manifest marks a paper available, the local MCP
downloads it from `OC_PAPER_CONTENT_BASE_URL` into `papers/paper.pdf`, verifies
the declared SHA-256 checksum, and records paper provenance plus
`redistribution_status` in `.openconstruction-manifest.json`. A missing manifest,
unpublished paper, or paper-transfer error is reported under `related_paper` but
does not fail a successfully downloaded dataset. Pass `include_papers: false`
to opt out for an individual job.

For the recommended `prepare_dataset_for_research` workflow, an available paper
must also finish before the Research Bundle is marked ready. If its transfer is
interrupted, the already completed dataset files remain verified and the next
preparation request resumes only the paper.

The paper registry defaults to
`Ben11304/OC-clone/open-construction-data/papers/manifest.json`. Its available
PDF objects are stored with Git LFS, so the default `OC_PAPER_CONTENT_BASE_URL`
uses GitHub's `media.githubusercontent.com` endpoint to resolve and download the
actual PDF instead of the small LFS pointer returned by `raw.githubusercontent.com`.
PDF binaries are not bundled in this MCP repository. A missing registry entry or
transfer error is reported under `related_paper` and the dataset download still
continues. An `unreviewed` rights state is deliberately preserved in plans and
manifests until evidence is recorded, so it can be audited without changing the
download protocol later.

The MCP honors `include_papers: true` independently from the informational
rights-review notice. Agents must not silently opt out of a paper because its
rights record is still under review. Verified entries additionally expose the
paper license, canonical license URL, review evidence, and review date. Users
can still explicitly pass `include_papers: false` for any individual download.

Provider authentication stays local to the user; OC OAuth and connected-account
brokerage are not required for dataset downloads. When a protected source has no
usable local credential, `download_dataset` returns `status: auth_required`,
provider-specific login steps, a security notice, and the exact safe tool payload
to retry. The agent should present those steps and wait for the user to complete
them in a local terminal. It must never ask the user to paste a token, password,
OAuth authorization code, cookie, or credential file into chat.

The built-in executable providers support direct HTTP files, `http_files`,
`zenodo_files`, `github_clone`, `huggingface_snapshot`, and `figshare_files`. Google Drive
folders, Dataverse collections, DesignSafe/Globus, DreamHouse, Roboflow,
Kaggle competitions, and Baidu share transfers are registered as assisted
providers and return `instructions_required` until their local executors are
implemented. Catalog-provided shell text is never executed.

### Download provider architecture

Acquisition planning and execution use the same provider registry under
`openconstruction_mcp.download_providers`. Each adapter owns its catalog
validation, capabilities, user instructions, and execution. `DownloadManager`
only supplies common safe primitives for resumable HTTP transfers, provider
metadata requests, progress checkpoints, checksums, cancellation, and final
manifests.

An additional provider subclasses `DownloadProvider` and can be registered at
runtime:

```python
from openconstruction_mcp.download_providers import (
    DownloadProvider,
    ProviderCapabilities,
    register_provider,
)

class ExampleProvider(DownloadProvider):
    method = "example_archive"
    provider_ids = ("example",)
    capabilities = ProviderCapabilities(
        access_mode="programmatic",
        executable=True,
        supports_resume=True,
    )

    def validate(self, metadata, distributions):
        if not metadata.get("resource_id"):
            raise ValueError("example_archive requires resource_id")

    def execute(self, context):
        # Resolve the provider resource, then use context.runtime.download_url(...).
        ...

register_provider(ExampleProvider())
```

Provider packages can register without application code changes through the
`openconstruction.download_providers` entry-point group:

```toml
[project.entry-points."openconstruction.download_providers"]
example = "example_package.provider:ExampleProvider"
```

The MCP plan includes the adapter's normalized `capabilities`, so CLI, MCP, and
website clients can render direct, programmatic, or assisted access without
hard-coding provider names.

Downloads use `OC_DATASETS_DIR` by default
(`~/.openconstruction/datasets`). The optional `destination` is one safe
directory name inside the selected library, and the optional `library_dir`
chooses another parent directory for that request. Set `OC_MAX_DOWNLOAD_BYTES` to cap a job's total
streamed HTTP transfer size and reject datasets whose declared size is above
the limit; the default is 500 GiB. Git and provider snapshots without declared
sizes cannot be fully checked before execution. Private Hugging Face datasets
recognize credentials saved by `hf auth login` as well as `HF_TOKEN` configured
directly in the local MCP process. Credential values are never included in MCP
tool results.

Remote HTTP MCP exposes `get_dataset_download_plan` but deliberately does not
expose tools that write files. A remote server cannot write into the user's
local filesystem; use the stdio MCP for execution.

## Skills

Skills are reusable workflows over the MCP tools. The repo-owned skill registry lives at:

- `skills/index.json`
- `skills/<skill-id>/metadata.json`

MCP clients can use `list_skills` or `get_skill`. If the repo remains private, the public website should use a published registry mirror or backend endpoint instead of reading GitHub raw files directly.

`dataset-discovery` is the first executable skill. It is available through `run_dataset_discovery` and returns ranked dataset candidates, fit reasons, checks, and suggested next actions.

To propose a new skill, open a GitHub issue with the skill proposal template. See [CONTRIBUTING.md](CONTRIBUTING.md) for metadata requirements, review checks, and pull request expectations.

Initial official skills focus on:

- dataset discovery
- dataset comparison
- model discovery
- schema explanation
- catalog gap analysis
- benchmark preparation

## Development

```bash
python scripts/validate_skills.py
python scripts/package_skills.py
python -m unittest discover -s tests
python scripts/smoke_stdio.py
```

Run the MCP server locally:

```bash
python -m openconstruction_mcp.server
```
