---
name: dataset-discovery
description: Find construction datasets for a specific AI or research task using OpenConstruction catalog metadata. Use when the user needs ranked dataset candidates, source links, license/access checks, or comparison-ready dataset options.
---

# Dataset Discovery

Use this skill to turn a dataset search request into a structured selection result.

## Inputs

Collect the user's task first. Add optional constraints when available:

- task
- modality
- object_class
- annotation
- license
- access_requirement

## Workflow

1. Build a focused catalog query from the user's constraints.
2. Search OpenConstruction dataset records.
3. Rank candidates by task fit, modality match, object/class match, annotation evidence, license, and access clarity.
4. Return the strongest candidates with catalog links, source URLs, fit reasons, and checks.
5. Suggest follow-up tool calls for inspecting or comparing candidates.

## Runtime

Call the MCP tool `run_dataset_discovery` with structured inputs. Use `get_resource` for deeper inspection of one dataset and `compare_resources` when the user wants to compare candidates.

## Output

Return:

- summary
- candidate_datasets
- selection_notes
- next_actions
