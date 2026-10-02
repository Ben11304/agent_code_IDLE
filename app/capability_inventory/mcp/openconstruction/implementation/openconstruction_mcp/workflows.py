from __future__ import annotations

from typing import Any

from .catalog import CatalogClient, clean_text, score_resource
from .skills import get_skill


DISCOVERY_FIELDS = ("task", "modality", "object_class", "annotation", "license", "access_requirement")
FIELD_WEIGHTS = {
    "task": 8,
    "modality": 8,
    "object_class": 30,
    "annotation": 14,
    "license": 10,
    "access_requirement": 8,
}
FIELD_MISS_PENALTIES = {
    "object_class": 18,
    "annotation": 8,
    "license": 6,
    "access_requirement": 4,
}
TERM_SYNONYMS = {
    "helmet": ["hardhat", "hardhats", "hard-hat", "safetyhelmet", "safety helmet"],
    "helmets": ["hardhat", "hardhats", "hard-hat", "safety helmets"],
    "hardhat": ["helmet", "helmets", "hardhats", "hard-hat", "safetyhelmet", "safety helmet"],
    "hardhats": ["helmet", "helmets", "hardhat", "hard-hat", "safety helmets"],
    "ppe": ["hardhat", "helmet", "vest", "glasses", "personal protective equipment"],
    "segmentation": ["mask", "masks", "semantic", "instance"],
    "bbox": ["bounding box", "boxes"],
    "boxes": ["bounding box", "bbox"],
}


def dataset_discovery(arguments: dict[str, Any], catalog: CatalogClient) -> dict[str, Any]:
    inputs = normalize_discovery_inputs(arguments)
    query = build_dataset_query(inputs)
    limit = bounded_limit(arguments.get("limit"), default=8, maximum=20)

    candidate_pool = collect_dataset_candidates(catalog, query, inputs, limit=max(limit * 4, 20))
    ranked = rank_dataset_candidates(candidate_pool, inputs)[:limit]
    candidates = [format_dataset_candidate(index + 1, item, inputs) for index, item in enumerate(ranked)]

    return {
        "skill": get_skill("dataset-discovery"),
        "inputs": inputs,
        "query": query,
        "summary": summarize_dataset_discovery(candidates, inputs),
        "candidate_datasets": candidates,
        "selection_notes": build_selection_notes(candidates, inputs),
        "next_actions": build_next_actions(candidates),
    }


def normalize_discovery_inputs(arguments: dict[str, Any]) -> dict[str, str]:
    return {field: clean_text(arguments.get(field), 180) for field in DISCOVERY_FIELDS if clean_text(arguments.get(field), 180)}


def build_dataset_query(inputs: dict[str, str]) -> str:
    ordered_terms = [inputs.get(field, "") for field in DISCOVERY_FIELDS]
    return " ".join(term for term in ordered_terms if term).strip() or "construction dataset"


def bounded_limit(value: Any, default: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(1, min(parsed, maximum))


def collect_dataset_candidates(catalog: CatalogClient, query: str, inputs: dict[str, str], limit: int) -> list[dict[str, Any]]:
    seen: set[str] = set()
    candidates: list[dict[str, Any]] = []

    searches = [query]
    searches.extend(value for value in inputs.values() if value and value != query)

    for search in searches:
        for item in catalog.search(search, ["dataset"], limit=limit):
            key = item["id"].lower()
            if key in seen:
                continue
            seen.add(key)
            candidates.append(item)

    if not candidates:
        candidates.extend(resource for resource in catalog.load() if resource.get("type") == "dataset")

    return candidates


def rank_dataset_candidates(candidates: list[dict[str, Any]], inputs: dict[str, str]) -> list[dict[str, Any]]:
    query = build_dataset_query(inputs)
    ranked = []
    for resource in candidates:
        fit = min(score_resource(resource, query), 18)
        matched = matched_constraints(resource, inputs)
        reasons = match_reasons(matched, inputs)
        missing = missing_dataset_fields(resource)
        for field in matched:
            fit += FIELD_WEIGHTS.get(field, 4)
            if exact_constraint_match(resource, field, inputs.get(field, "")):
                fit += 12 if field == "object_class" else 5
        for field, penalty in FIELD_MISS_PENALTIES.items():
            if inputs.get(field) and field not in matched:
                fit -= penalty
        fit -= len(missing)
        ranked.append({**resource, "fit_score": max(fit, 0), "match_reasons": reasons, "missing_fields": missing})
    ranked.sort(key=lambda item: (-item["fit_score"], item["title"]))
    return ranked


def matched_constraints(resource: dict[str, Any], inputs: dict[str, str]) -> set[str]:
    text = searchable_text(resource)
    matched = set()
    for field, value in inputs.items():
        terms = terms_for_value(value)
        if terms and any(term in text for term in terms):
            matched.add(field)
    return matched


def exact_constraint_match(resource: dict[str, Any], field: str, value: str) -> bool:
    if not value:
        return False
    text = searchable_text(resource)
    exact = clean_text(value, 180).lower()
    if exact and exact in text:
        return True
    if field != "object_class":
        return False
    return any(term in text for term in value.lower().replace("_", " ").replace("-", " ").split() if len(term) > 2)


def match_reasons(matched: set[str], inputs: dict[str, str]) -> list[str]:
    reasons = []
    labels = {
        "task": "task",
        "modality": "modality",
        "object_class": "object/class",
        "annotation": "annotation",
        "license": "license",
        "access_requirement": "access requirement",
    }
    for field, label in labels.items():
        value = inputs.get(field)
        if value and field in matched:
            reasons.append(f"Matches {label}: {value}")
    return reasons


def terms_for_value(value: str) -> list[str]:
    terms = [term for term in value.lower().replace("_", " ").replace("-", " ").split() if len(term) > 1]
    expanded = set(terms)
    cleaned_value = clean_text(value, 180).lower()
    if cleaned_value:
        expanded.add(cleaned_value)
    for term in terms:
        expanded.update(TERM_SYNONYMS.get(term, []))
    return sorted(expanded, key=len, reverse=True)


def searchable_text(resource: dict[str, Any]) -> str:
    parts = [
        resource.get("title"),
        resource.get("summary"),
        resource.get("evidence"),
        resource.get("license"),
        resource.get("url"),
        resource.get("href"),
        resource.get("tags"),
        resource.get("source"),
    ]
    return clean_text(parts, 3000).lower()


def missing_dataset_fields(resource: dict[str, Any]) -> list[str]:
    missing = []
    if not resource.get("license"):
        missing.append("license")
    if not resource.get("url") and not resource.get("href"):
        missing.append("access_link")
    if not resource.get("summary"):
        missing.append("summary")
    if not resource.get("year"):
        missing.append("year")
    return missing


def format_dataset_candidate(rank: int, resource: dict[str, Any], inputs: dict[str, str]) -> dict[str, Any]:
    return {
        "rank": rank,
        "id": resource.get("id"),
        "title": resource.get("title"),
        "summary": resource.get("summary"),
        "href": resource.get("href"),
        "source_url": resource.get("url"),
        "year": resource.get("year"),
        "license": resource.get("license"),
        "tags": resource.get("tags", [])[:10],
        "fit_score": resource.get("fit_score", 0),
        "fit_reasons": resource.get("match_reasons", []) or ["Matched the dataset discovery query."],
        "checks": dataset_checks(resource, inputs),
    }


def dataset_checks(resource: dict[str, Any], inputs: dict[str, str]) -> list[str]:
    checks = []
    missing = resource.get("missing_fields", [])
    if missing:
        checks.append("Verify missing metadata: " + ", ".join(missing))
    if inputs.get("license") and not resource.get("license"):
        checks.append("Confirm license before reuse.")
    if inputs.get("annotation") and not any("annotation" in reason.lower() for reason in resource.get("match_reasons", [])):
        checks.append("Confirm annotation format in the source record.")
    if inputs.get("access_requirement") and not resource.get("url"):
        checks.append("Confirm dataset access route from the linked catalog page.")
    return checks or ["Review source page before reuse."]


def summarize_dataset_discovery(candidates: list[dict[str, Any]], inputs: dict[str, str]) -> str:
    if not candidates:
        return "No matching dataset candidates were found for the requested constraints."
    task = inputs.get("task", "the requested task")
    top = candidates[0]["title"]
    return f"Found {len(candidates)} dataset candidate(s) for {task}. The strongest match is {top}."


def build_selection_notes(candidates: list[dict[str, Any]], inputs: dict[str, str]) -> list[str]:
    if not candidates:
        return ["Try broadening the task, modality, object class, or annotation constraints."]

    notes = ["Rank candidates by task fit, modality match, annotation evidence, license, and access clarity."]
    if inputs.get("license"):
        notes.append("License preference was included in ranking; still confirm license terms from the source.")
    if any(candidate["checks"] for candidate in candidates):
        notes.append("Use the checks on each candidate before selecting a dataset.")
    return notes


def build_next_actions(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    actions = []
    for candidate in candidates[:3]:
        actions.append(
            {
                "label": f"Inspect {candidate['title']}",
                "tool": "get_resource",
                "arguments": {"type": "dataset", "id": candidate["id"]},
            }
        )
    if len(candidates) >= 2:
        actions.append(
            {
                "label": "Compare top dataset candidates",
                "tool": "compare_resources",
                "arguments": {
                    "resources": [{"type": "dataset", "id": candidate["id"]} for candidate in candidates[:3]],
                    "goal": "dataset selection",
                },
            }
        )
    return actions
