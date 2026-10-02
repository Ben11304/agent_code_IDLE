# Contributing OpenConstruction Skills

OpenConstruction skills are reusable task workflows over the shared API and MCP tools. A prompt can be an example request, but the skill should define the procedure, inputs, outputs, and review checks behind that request.

Submit a public source URL for review before opening a pull request. Good source URLs point to a GitHub skill folder, a ZIP package, or documentation that includes the expected skill files.

## What Makes A Useful Skill

A skill should include:

- a clear user task
- structured inputs
- the API or MCP tools it calls
- ranking, validation, or decision logic
- structured outputs users can act on
- tests or examples that show the workflow works

## Skill Layout

```text
skills/
  dataset-discovery/
    SKILL.md
    metadata.json
    examples/
    tests/
```

Every skill must be listed in `skills/index.json`.

`metadata.json` is the registry record. `SKILL.md` is the agent instruction file. A complete accepted skill normally includes both, plus examples and tests when runtime behavior is added.

## Required Metadata

Each `metadata.json` entry must include:

- `id`
- `name`
- `author`
- `version`
- `tier`
- `status`
- `lifecycle_stage`
- `description`
- `inputs`
- `outputs`
- `tools`
- `permissions`
- `risk_level`
- `license`
- `review`

If the skill has runtime logic, include:

- `runtime.tool`
- `runtime.module`
- `runtime.function`

## Review Checklist

Maintainers review skill submissions for:

- concrete user value
- source URL with reviewable skill files
- valid metadata
- clear lifecycle stage
- correct tool declarations
- permission declarations
- working examples
- tests for runtime logic
- license, copyright, and permission to share
- no private data
- no hidden external access
- no code execution unless explicitly declared

## Preferred First Step

Open a GitHub issue using the skill proposal template before opening a pull request. The maintainers can help refine scope before code is written.

## Pull Request Checklist

Before submitting a pull request:

- add or update `skills/<skill-id>/metadata.json`
- add or update `skills/<skill-id>/SKILL.md`
- add runtime code when the skill needs behavior beyond search
- update `skills/index.json`
- add tests
- run `python scripts/validate_skills.py`
- run `python -m unittest discover -s tests`
- run `python scripts/smoke_stdio.py`
