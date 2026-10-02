from __future__ import annotations

import json
import sys
from typing import Any, Callable

from .acquisition import resolve_dataset_download_plan
from .catalog import CatalogClient
from .downloads import DownloadManager
from .papers import DEFAULT_INCLUDE_PAPERS, PaperCatalogClient
from .research import find_research_bundle, find_research_bundle_for_dataset
from .skills import get_skill, list_skills
from .workflows import dataset_discovery


PROTOCOL_VERSION = "2025-06-18"
SERVER_INFO = {"name": "openconstruction", "version": "0.1.0"}

catalog = CatalogClient()
paper_catalog = PaperCatalogClient()
download_manager = DownloadManager()


def schema(properties: dict[str, Any] | None = None, required: list[str] | None = None) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties or {},
        "required": required or [],
        "additionalProperties": False,
    }


RESOURCE_TYPES = ["dataset", "model", "workflow", "oer", "tool", "guide", "contributor", "benchmark", "vocabulary"]

TOOLS: list[dict[str, Any]] = [
    {
        "name": "search_resources",
        "title": "Search OpenConstruction resources",
        "description": "Search datasets, models, workflows, OERs, tools, guides, contributors, benchmarks, and vocabulary.",
        "inputSchema": schema(
            {
                "query": {"type": "string", "minLength": 1},
                "types": {"type": "array", "items": {"type": "string", "enum": RESOURCE_TYPES}},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100},
            },
            ["query"],
        ),
    },
    {
        "name": "get_resource",
        "title": "Get OpenConstruction resource",
        "description": "Get one resource by type and id.",
        "inputSchema": schema(
            {
                "type": {"type": "string", "enum": RESOURCE_TYPES},
                "id": {"type": "string", "minLength": 1},
            },
            ["type", "id"],
        ),
    },
    {
        "name": "compare_resources",
        "title": "Compare OpenConstruction resources",
        "description": "Compare two or more catalog resources for a user goal.",
        "inputSchema": schema(
            {
                "resources": {
                    "type": "array",
                    "minItems": 2,
                    "maxItems": 8,
                    "items": schema(
                        {
                            "type": {"type": "string", "enum": RESOURCE_TYPES},
                            "id": {"type": "string", "minLength": 1},
                        },
                        ["type", "id"],
                    ),
                },
                "goal": {"type": "string"},
            },
            ["resources"],
        ),
    },
    {
        "name": "get_catalog_stats",
        "title": "Get catalog statistics",
        "description": "Return counts by resource type for the loaded catalog snapshot.",
        "inputSchema": schema(),
    },
    {
        "name": "ask_openconstruction",
        "title": "Ask OpenConstruction",
        "description": "Answer a catalog-grounded question with matching resources.",
        "inputSchema": schema({"query": {"type": "string", "minLength": 1}}, ["query"]),
    },
    {
        "name": "find_datasets",
        "title": "Dataset Discovery",
        "description": "Find datasets by task, modality, object class, annotation type, license, or access needs.",
        "inputSchema": schema(
            {
                "task": {"type": "string"},
                "modality": {"type": "string"},
                "object_class": {"type": "string"},
                "annotation": {"type": "string"},
                "license": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50},
            }
        ),
    },
    {
        "name": "run_dataset_discovery",
        "title": "Run Dataset Discovery Skill",
        "description": "Run the Dataset Discovery skill as a structured workflow that searches, ranks, and explains dataset candidates.",
        "inputSchema": schema(
            {
                "task": {"type": "string", "minLength": 1},
                "modality": {"type": "string"},
                "object_class": {"type": "string"},
                "annotation": {"type": "string"},
                "license": {"type": "string"},
                "access_requirement": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 20},
            },
            ["task"],
        ),
    },
    {
        "name": "find_models",
        "title": "Model Discovery",
        "description": "Find models by task, dataset, modality, method, or publication evidence.",
        "inputSchema": schema(
            {
                "task": {"type": "string"},
                "dataset": {"type": "string"},
                "modality": {"type": "string"},
                "method": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50},
            }
        ),
    },
    {
        "name": "explain_schema",
        "title": "Schema Explainer",
        "description": "Explain common OpenConstruction metadata fields and where they appear in catalog records.",
        "inputSchema": schema({"field": {"type": "string"}, "resource_type": {"type": "string", "enum": RESOURCE_TYPES}}),
    },
    {
        "name": "analyze_catalog_gaps",
        "title": "Catalog Gap Analysis",
        "description": "Inspect catalog coverage by type and return possible metadata gaps for a topic.",
        "inputSchema": schema({"topic": {"type": "string"}, "limit": {"type": "integer", "minimum": 1, "maximum": 50}}),
    },
    {
        "name": "prepare_benchmark_submission",
        "title": "Benchmark Submission Prep",
        "description": "Create a checklist for preparing benchmark metadata and linked resources.",
        "inputSchema": schema(
            {
                "dataset_id": {"type": "string"},
                "model_id": {"type": "string"},
                "task": {"type": "string"},
                "metric": {"type": "string"},
            },
            ["task"],
        ),
    },
    {
        "name": "validate_metadata_record",
        "title": "Metadata Validation",
        "description": "Check one draft metadata record for important missing fields.",
        "inputSchema": schema({"record": {"type": "object"}, "resource_type": {"type": "string", "enum": RESOURCE_TYPES}}, ["record", "resource_type"]),
    },
    {
        "name": "list_skills",
        "title": "List OpenConstruction skills",
        "description": "List repo-defined OpenConstruction skill metadata.",
        "inputSchema": schema(
            {
                "lifecycle_stage": {
                    "type": "string",
                    "enum": ["discover", "select", "access", "understand", "analyze", "execute"],
                }
            }
        ),
    },
    {
        "name": "get_skill",
        "title": "Get OpenConstruction skill",
        "description": "Get one OpenConstruction skill definition by id.",
        "inputSchema": schema({"id": {"type": "string", "minLength": 1}}, ["id"]),
    },
    {
        "name": "get_dataset_download_plan",
        "title": "Get Dataset Download Plan",
        "description": "Resolve the trusted OpenConstruction dataset and related-paper routes without writing files, including local provider-auth guidance when required.",
        "inputSchema": schema(
            {
                "dataset_id": {"type": "string", "minLength": 1},
                "include_papers": {
                    "type": "boolean",
                    "default": True,
                    "description": "Include the related OC-hosted paper plan. Defaults to true; set false only when the user explicitly opts out.",
                },
            },
            ["dataset_id"],
        ),
    },
    {
        "name": "get_dataset_paper_plan",
        "title": "Get Related Paper Plan",
        "description": "Return the OC paper metadata, provenance, rights-review status, source URL, checksum, and expected size for one dataset.",
        "inputSchema": schema({"dataset_id": {"type": "string", "minLength": 1}}, ["dataset_id"]),
    },
    {
        "name": "check_dataset_installation",
        "title": "Check Dataset Installation",
        "description": "Check whether a matching dataset package is installed, partially downloaded and resumable, absent, or conflicts with another source at the selected OC library location.",
        "inputSchema": schema(
            {
                "dataset_id": {"type": "string", "minLength": 1},
                "destination": {
                    "type": "string",
                    "maxLength": 160,
                    "pattern": "^[A-Za-z0-9._-]+$",
                    "description": "Optional dataset directory name. Defaults to the dataset id.",
                },
                "library_dir": {
                    "type": "string",
                    "maxLength": 1024,
                    "description": "Optional custom parent directory for installed OC datasets. Defaults to OC_DATASETS_DIR or ~/.openconstruction/datasets.",
                },
                "include_papers": {"type": "boolean", "default": True},
                "verify_checksums": {
                    "type": "boolean",
                    "default": False,
                    "description": "Recompute SHA-256 for installed HTTP files. More reliable but potentially slow for large datasets.",
                },
            },
            ["dataset_id"],
        ),
    },
    {
        "name": "prepare_dataset_for_research",
        "title": "Prepare Dataset for Research",
        "description": "Recommended one-step workflow for non-technical researchers. Reuse an exact completed package or resume a matching checkpoint automatically; otherwise review the package, accept the dataset license, download the dataset plus original related PDF, and create a provenance-tracked Research Bundle. Papers are included by default and are never converted to Markdown.",
        "inputSchema": schema(
            {
                "dataset_id": {"type": "string", "minLength": 1},
                "accept_license": {
                    "type": "boolean",
                    "default": False,
                    "description": "Set true only after the user reviews and accepts the dataset license and source terms. Omit or set false to receive a plain-language review first.",
                },
                "library_dir": {
                    "type": "string",
                    "maxLength": 1024,
                    "description": "Optional custom parent directory. Defaults to OC_DATASETS_DIR or ~/.openconstruction/datasets.",
                },
            },
            ["dataset_id"],
        ),
    },
    {
        "name": "get_research_preparation_status",
        "title": "Get Research Preparation Status",
        "description": "Show restart-safe progress for a research package and return its dataset, original PDF, licenses, and bundle location when ready. An interrupted result includes the safe retry that resumes saved progress.",
        "inputSchema": schema({"preparation_id": {"type": "string", "minLength": 1}}, ["preparation_id"]),
    },
    {
        "name": "download_dataset",
        "title": "Download Dataset Locally",
        "description": "Install, reuse, or resume a dataset in the default OC library or an explicitly selected library_dir. Matching verified files are reused and HTTP partial files resume when supported. If status is auth_required, show its local provider login instructions and retry payload; never ask the user to paste credentials into chat. Requires explicit license acceptance for a new source and never accepts arbitrary source URLs or shell commands.",
        "inputSchema": schema(
            {
                "dataset_id": {"type": "string", "minLength": 1},
                "destination": {
                    "type": "string",
                    "maxLength": 160,
                    "pattern": "^[A-Za-z0-9._-]+$",
                    "description": "Optional dataset directory name. Defaults to the dataset id.",
                },
                "library_dir": {
                    "type": "string",
                    "maxLength": 1024,
                    "description": "Optional custom parent directory. Defaults to OC_DATASETS_DIR or ~/.openconstruction/datasets.",
                },
                "accept_license": {
                    "type": "boolean",
                    "description": "True only after the user reviews and accepts the dataset license and source terms.",
                },
                "include_papers": {
                    "type": "boolean",
                    "default": True,
                    "description": "Download the related paper from the configured OC source. Defaults to true; never set false solely because a rights-review notice is present.",
                },
            },
            ["dataset_id", "accept_license"],
        ),
    },
    {
        "name": "get_download_status",
        "title": "Get Dataset Download Status",
        "description": "Return agent-ready progress text, percentage, bar, speed, ETA, files, and errors for a local OC dataset download job.",
        "inputSchema": schema({"download_id": {"type": "string", "minLength": 1}}, ["download_id"]),
    },
    {
        "name": "cancel_download",
        "title": "Cancel Dataset Download",
        "description": "Request cancellation of a running local OC dataset download job.",
        "inputSchema": schema({"download_id": {"type": "string", "minLength": 1}}, ["download_id"]),
    },
]

LOCAL_ONLY_TOOLS = {
    "check_dataset_installation",
    "prepare_dataset_for_research",
    "get_research_preparation_status",
    "download_dataset",
    "get_download_status",
    "cancel_download",
}


def text_content(value: Any) -> dict[str, Any]:
    return {
        "content": [
            {
                "type": "text",
                "text": json.dumps(value, ensure_ascii=False, indent=2) if not isinstance(value, str) else value,
            }
        ],
        "isError": False,
    }


def query_from_args(arguments: dict[str, Any], keys: list[str]) -> str:
    return " ".join(str(arguments.get(key, "")) for key in keys if arguments.get(key)).strip()


def include_papers_from_args(arguments: dict[str, Any]) -> bool:
    return arguments.get("include_papers", DEFAULT_INCLUDE_PAPERS) is not False


def research_license_review(
    plan: Any,
    paper_plan: dict[str, Any],
    *,
    library_dir: str | None = None,
) -> dict[str, Any]:
    dataset_size = plan.estimated_size if isinstance(plan.estimated_size, int) else None
    paper_size = paper_plan.get("content_size") if paper_plan.get("available") else None
    total_size = dataset_size + paper_size if isinstance(dataset_size, int) and isinstance(paper_size, int) else None
    return {
        "status": "license_acceptance_required",
        "dataset_id": plan.dataset_id,
        "dataset_name": plan.dataset_name,
        "package_contents": {
            "dataset": True,
            "related_paper": bool(paper_plan.get("available")),
            "original_pdf_preserved": True,
            "pdf_to_markdown": False,
        },
        "estimated_total_bytes": total_size,
        "license_review": {
            "dataset_license": plan.license,
            "dataset_source": plan.url,
            "paper_title": paper_plan.get("paper_title"),
            "paper_license": paper_plan.get("paper_license"),
            "paper_rights_status": paper_plan.get("redistribution_status"),
            "paper_license_url": paper_plan.get("paper_license_url"),
        },
        "confirmation_prompt": (
            f"This research package includes \"{plan.dataset_name}\" and its related original PDF when available. "
            f"The dataset license is {plan.license or 'not specified'}. Ask the user to review these terms and confirm before continuing."
        ),
        "next_step": {
            "tool": "prepare_dataset_for_research",
            "arguments": {
                "dataset_id": plan.dataset_id,
                "accept_license": True,
                **({"library_dir": library_dir} if library_dir else {}),
            },
        },
    }


def research_preparation_view(result: dict[str, Any]) -> dict[str, Any]:
    status = result.get("status")
    dataset_name = result.get("dataset_name") or result.get("dataset_id") or "Dataset"
    if status == "completed":
        message = f"{dataset_name} is ready for research with its original related PDF when available."
    elif status == "auth_required":
        message = "Local provider authentication is required before research preparation can continue."
    elif status == "failed":
        message = f"Research preparation failed: {result.get('error') or 'unknown error'}"
    elif status == "cancelled":
        message = "Research preparation was cancelled."
    elif status == "interrupted":
        message = "Research preparation is paused with saved progress and will resume from its checkpoint."
    elif status == "source_changed":
        message = "The local files belong to a different dataset source/version, so OpenConstruction will not overwrite them."
    elif status == "already_running" or result.get("already_running") is True:
        message = "Another OpenConstruction process is already preparing this research package."
    else:
        message = result.get("progress_text") or "Preparing the research package."
    paper = result.get("related_paper") or {}
    return {
        "preparation_id": result.get("download_id"),
        "dataset_id": result.get("dataset_id"),
        "dataset_name": result.get("dataset_name"),
        "status": status,
        "user_message": message,
        "progress_percent": result.get("progress_percent"),
        "progress_bar": result.get("progress_bar"),
        "progress_text": result.get("progress_text"),
        "speed_bytes_per_second": result.get("speed_bytes_per_second"),
        "eta_seconds": result.get("eta_seconds"),
        "package_contents": {
            "dataset": True,
            "related_paper": paper.get("download_status"),
            "paper_title": paper.get("paper_title"),
            "paper_license": paper.get("paper_license"),
            "paper_rights_status": paper.get("redistribution_status"),
            "original_pdf_preserved": True,
            "pdf_to_markdown": False,
        },
        "research_bundle": result.get("research_bundle"),
        "already_running": result.get("already_running") is True,
        "resumed": result.get("resumed") is True,
        "resume_available": result.get("resume_available") is True,
        "source_fingerprint": result.get("source_fingerprint"),
        "destination": result.get("destination"),
        "warnings": result.get("warnings") or [],
        "error": result.get("error"),
        "auth": result.get("auth"),
        "retry": result.get("retry"),
    }


def persisted_research_result(bundle: dict[str, Any]) -> dict[str, Any]:
    papers = bundle.get("papers") or []
    paper = papers[0] if papers else {}
    return {
        "download_id": bundle.get("preparation_id"),
        "dataset_id": bundle.get("dataset_id"),
        "dataset_name": bundle.get("dataset_name"),
        "status": "completed",
        "progress_percent": 100.0,
        "progress_bar": "[████████████████████]",
        "progress_text": "Ready [████████████████████] 100.0% — completed research package found on disk",
        "speed_bytes_per_second": None,
        "eta_seconds": 0,
        "destination": bundle.get("dataset_path"),
        "related_paper": {
            "download_status": "completed" if paper else "unavailable",
            "paper_title": paper.get("title"),
            "paper_license": paper.get("license"),
            "redistribution_status": paper.get("redistribution_status"),
        },
        "research_bundle": bundle,
        "warnings": [],
    }


def call_tool(name: str, arguments: dict[str, Any], allow_download_execution: bool = True) -> Any:
    if name == "search_resources":
        return {"resources": catalog.search(arguments["query"], arguments.get("types"), int(arguments.get("limit", 20)))}
    if name == "get_resource":
        return {"resource": catalog.get(arguments["type"], arguments["id"])}
    if name == "compare_resources":
        return catalog.compare(arguments["resources"], arguments.get("goal", ""))
    if name == "get_catalog_stats":
        return catalog.stats()
    if name == "ask_openconstruction":
        return catalog.answer(arguments["query"])
    if name == "find_datasets":
        query = query_from_args(arguments, ["task", "modality", "object_class", "annotation", "license"])
        return {"resources": catalog.search(query or "dataset", ["dataset"], int(arguments.get("limit", 12)))}
    if name == "run_dataset_discovery":
        return dataset_discovery(arguments, catalog)
    if name == "find_models":
        query = query_from_args(arguments, ["task", "dataset", "modality", "method"])
        return {"resources": catalog.search(query or "model", ["model"], int(arguments.get("limit", 12)))}
    if name == "explain_schema":
        return explain_schema(arguments.get("field", ""), arguments.get("resource_type", ""))
    if name == "analyze_catalog_gaps":
        return analyze_catalog_gaps(arguments.get("topic", ""), int(arguments.get("limit", 20)))
    if name == "prepare_benchmark_submission":
        return prepare_benchmark_submission(arguments)
    if name == "validate_metadata_record":
        return validate_metadata_record(arguments["record"], arguments["resource_type"])
    if name == "list_skills":
        return list_skills(arguments.get("lifecycle_stage"))
    if name == "get_skill":
        return {"skill": get_skill(arguments["id"])}
    if name == "get_dataset_download_plan":
        plan = resolve_dataset_download_plan(catalog, arguments["dataset_id"])
        include_papers = include_papers_from_args(arguments)
        return {
            "plan": plan.to_dict(),
            "include_papers": include_papers,
            "paper_plan": paper_catalog.resolve(plan.dataset_id) if include_papers else None,
        }
    if name == "get_dataset_paper_plan":
        plan = resolve_dataset_download_plan(catalog, arguments["dataset_id"])
        return {"paper_plan": paper_catalog.resolve(plan.dataset_id)}
    if name == "check_dataset_installation":
        if not allow_download_execution:
            raise ValueError("Dataset installations can be inspected only by the local stdio MCP")
        plan = resolve_dataset_download_plan(catalog, arguments["dataset_id"])
        include_papers = include_papers_from_args(arguments)
        paper_plan = paper_catalog.resolve(plan.dataset_id) if include_papers else None
        return download_manager.inspect_installation(
            plan,
            arguments.get("destination"),
            library_dir=arguments.get("library_dir"),
            include_papers=include_papers,
            paper_plan=paper_plan,
            verify_checksums=arguments.get("verify_checksums") is True,
        )
    if name == "prepare_dataset_for_research":
        if not allow_download_execution:
            raise ValueError("Research packages can be prepared only by the local stdio MCP")
        plan = resolve_dataset_download_plan(catalog, arguments["dataset_id"])
        paper_plan = paper_catalog.resolve(plan.dataset_id)
        library_dir = arguments.get("library_dir")
        selected_root = download_manager.store.resolve_library_dir(library_dir)
        fingerprint = download_manager.source_fingerprint(plan, include_papers=True, paper_plan=paper_plan)
        existing = find_research_bundle_for_dataset(selected_root, plan.dataset_id, fingerprint)
        if existing:
            return research_preparation_view(persisted_research_result(existing))
        if plan.executable_locally and download_manager.has_accepted_resume(
            plan,
            None,
            include_papers=True,
            paper_plan=paper_plan,
            library_dir=library_dir,
        ):
            return research_preparation_view(
                download_manager.start(
                    plan,
                    None,
                    False,
                    include_papers=True,
                    paper_plan=paper_plan,
                    create_bundle=True,
                    library_dir=library_dir,
                )
            )
        if arguments.get("accept_license") is not True:
            return research_license_review(plan, paper_plan, library_dir=library_dir)
        if not plan.executable_locally:
            result = download_manager.start(
                plan,
                None,
                True,
                include_papers=True,
                paper_plan=paper_plan,
                create_bundle=True,
                library_dir=library_dir,
            )
            return {
                "preparation_id": None,
                "dataset_id": plan.dataset_id,
                "dataset_name": plan.dataset_name,
                "status": "instructions_required",
                "user_message": "This source needs one external download step before OpenConstruction can prepare the research package.",
                "package_contents": {
                    "dataset": True,
                    "related_paper": bool(paper_plan.get("available")),
                    "original_pdf_preserved": True,
                    "pdf_to_markdown": False,
                },
                "instructions": (result.get("plan") or {}).get("instructions"),
                "auth": result.get("auth"),
            }
        result = download_manager.start(
            plan,
            None,
            True,
            include_papers=True,
            paper_plan=paper_plan,
            create_bundle=True,
            library_dir=library_dir,
        )
        if result.get("status") == "auth_required" and isinstance(result.get("retry"), dict):
            result["retry"] = {
                "tool": "prepare_dataset_for_research",
                "arguments": {
                    "dataset_id": plan.dataset_id,
                    "accept_license": True,
                    **({"library_dir": library_dir} if library_dir else {}),
                },
            }
        return research_preparation_view(result)
    if name == "get_research_preparation_status":
        if not allow_download_execution:
            raise ValueError("Research preparation jobs are available only on the local stdio MCP")
        preparation_id = arguments["preparation_id"]
        try:
            result = download_manager.get(preparation_id)
        except ValueError:
            persisted = find_research_bundle(download_manager.root, preparation_id)
            if not persisted:
                raise
            result = persisted_research_result(persisted)
        if result.get("workflow") not in {None, "research_preparation"}:
            raise ValueError("The id belongs to a dataset download, not a research preparation")
        return research_preparation_view(result)
    if name == "download_dataset":
        if not allow_download_execution:
            raise ValueError("Remote MCP cannot write to the user's filesystem; use the local stdio MCP to execute downloads")
        plan = resolve_dataset_download_plan(catalog, arguments["dataset_id"])
        include_papers = include_papers_from_args(arguments)
        paper_plan = paper_catalog.resolve(plan.dataset_id) if include_papers else None
        return download_manager.start(
            plan,
            arguments.get("destination"),
            arguments.get("accept_license") is True,
            include_papers=include_papers,
            paper_plan=paper_plan,
            library_dir=arguments.get("library_dir"),
        )
    if name == "get_download_status":
        if not allow_download_execution:
            raise ValueError("Download jobs are available only on the local stdio MCP")
        return download_manager.get(arguments["download_id"])
    if name == "cancel_download":
        if not allow_download_execution:
            raise ValueError("Download jobs are available only on the local stdio MCP")
        return download_manager.cancel(arguments["download_id"])
    raise ValueError(f"Unknown tool: {name}")


def explain_schema(field: str, resource_type: str) -> dict[str, Any]:
    field_text = field.strip()
    examples = [item for item in catalog.load() if not resource_type or item["type"] == resource_type][:8]
    observed = sorted({key for item in examples for key in item.get("source", {}).keys()})
    return {
        "field": field_text or None,
        "resource_type": resource_type or None,
        "observed_fields": observed,
        "summary": "OpenConstruction metadata fields describe resource identity, provenance, task fit, access, licensing, and links back to source material.",
        "matching_examples": [item for item in examples if not field_text or field_text in item.get("source", {})][:5],
    }


def analyze_catalog_gaps(topic: str, limit: int) -> dict[str, Any]:
    matches = catalog.search(topic or "construction", limit=limit)
    stats = catalog.stats()
    sparse = [resource_type for resource_type, count in stats["by_type"].items() if count < 5]
    return {
        "topic": topic,
        "matching_resources": matches,
        "counts_by_type": stats["by_type"],
        "possible_gaps": sparse,
        "notes": [
            "Review records with missing license, year, source URL, or task fields.",
            "Compare coverage across datasets, models, workflows, and benchmarks for the topic.",
        ],
    }


def prepare_benchmark_submission(arguments: dict[str, Any]) -> dict[str, Any]:
    dataset = catalog.get("dataset", arguments.get("dataset_id", "")) if arguments.get("dataset_id") else None
    model = catalog.get("model", arguments.get("model_id", "")) if arguments.get("model_id") else None
    return {
        "task": arguments.get("task"),
        "metric": arguments.get("metric"),
        "linked_dataset": dataset,
        "linked_model": model,
        "checklist": [
            "Define task and evaluation split",
            "Identify dataset and model records",
            "List metrics and reporting units",
            "Add source paper or repository links",
            "Include license and citation fields",
            "Document validation notes",
        ],
    }


def validate_metadata_record(record: dict[str, Any], resource_type: str) -> dict[str, Any]:
    required_by_type = {
        "dataset": ["name", "year", "license"],
        "model": ["title", "authors", "year"],
        "workflow": ["title", "summary"],
        "oer": ["title", "provider", "license"],
        "tool": ["name", "url", "summary"],
    }
    required = required_by_type.get(resource_type, ["title"])
    missing = [field for field in required if not record.get(field)]
    recommended = [field for field in ("summary", "url", "doi", "tasks", "modalities", "license") if not record.get(field)]
    return {
        "resource_type": resource_type,
        "valid": not missing,
        "missing_required": missing,
        "missing_recommended": recommended,
    }


def handle_request(message: dict[str, Any], allow_download_execution: bool = True) -> dict[str, Any] | None:
    method = message.get("method")
    request_id = message.get("id")

    if request_id is None:
        return None

    try:
        if method == "initialize":
            return response(
                request_id,
                {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": SERVER_INFO,
                    "instructions": (
                        "Use OpenConstruction to discover and understand public AEC datasets and related resources. "
                        "For a normal request to download or prepare a dataset, prefer prepare_dataset_for_research: first call it "
                        "without acceptance to present its plain-language license review, then call it with accept_license=true only "
                        "after the user explicitly agrees. Poll get_research_preparation_status and show progress_text at most every "
                        "two seconds until completion. Present one simple research-package workflow and do not expose internal tool "
                        "sequencing, checksums, paths, or bundle implementation details unless the user asks. Reuse a returned ready "
                        "package immediately. If saved progress is interrupted, call its retry or call prepare_dataset_for_research "
                        "again: the MCP resumes the matching checkpoint and must not ask the user to accept the same license again. "
                        "Never overwrite a source_changed destination. Related papers are included by default and "
                        "downloaded from the configured OC paper source. Preserve include_papers=true unless the user explicitly "
                        "opts out; never disable it solely because a rights-review notice is present. Report the paper license, "
                        "rights-review status, and any warning without changing the requested download scope. Preserve original PDFs; "
                        "do not claim that OC converts them to Markdown or extracts their content. Use get_dataset_download_plan and "
                        "download_dataset only for advanced or explicitly requested low-level control. If a job returns auth_required, "
                        "show the returned local provider-login instructions, never ask for credentials in chat, "
                        "wait for the user to authenticate locally, and then use the returned retry payload."
                    ),
                },
            )
        if method == "server/discover":
            return response(
                request_id,
                {
                    "protocolVersion": "2026-07-28",
                    "supportedVersions": ["2026-07-28", "2025-06-18", "2025-03-26"],
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": SERVER_INFO,
                },
            )
        if method == "ping":
            return response(request_id, {})
        if method == "tools/list":
            tools = TOOLS if allow_download_execution else [tool for tool in TOOLS if tool["name"] not in LOCAL_ONLY_TOOLS]
            return response(request_id, {"tools": tools})
        if method == "tools/call":
            params = message.get("params") or {}
            result = call_tool(params.get("name", ""), params.get("arguments") or {}, allow_download_execution)
            return response(request_id, text_content(result))
        return error(request_id, -32601, f"Method not found: {method}")
    except Exception as exc:
        return error(request_id, -32000, str(exc))


def response(request_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def serve(input_stream: Any = sys.stdin, output_stream: Any = sys.stdout) -> None:
    for line in input_stream:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
            result = handle_request(message)
        except Exception as exc:
            result = error(None, -32700, f"Parse error: {exc}")
        if result is not None:
            output_stream.write(json.dumps(result, ensure_ascii=False, separators=(",", ":")) + "\n")
            output_stream.flush()


def main() -> None:
    serve()


if __name__ == "__main__":
    main()
