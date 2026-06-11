# Research Integrity Rules (shared)

**MANDATORY — highest priority, overrides every other instruction.**
These are project-wide rules; every agent reads them first at pre-flight.

## Rule 0 — DO NOT ANSWER WHEN INFORMATION IS MISSING
**This is the most important rule, ahead of every other rule.**
- Missing input / scope / threshold / version / schema / intent → **STOP, ASK**.
- Do NOT guess, do NOT fill in with implicit defaults, do NOT extrapolate.
- Ask specifically: list exactly what is missing and in what form it is needed.
- Violating this rule = violating research integrity (it leads to fabricated numbers, wrong thresholds, wrong citations).

1. **Do NOT decide the conclusion first and then find papers to support it.** Correct order: data → pattern → literature. If none exists → write *"no literature support, empirical observation."*

2. **Do NOT pick a threshold/parameter and then justify it backwards.** Correct: literature → threshold. If none exists → sensitivity analysis across multiple thresholds.

3. **Citing a paper**: must read/verify it. Do NOT extrapolate from the title/abstract. Unverified → write *"cited based on abstract only."*

4. **Unexpected results**: do NOT explain them away. Report as-is.

5. **Distinguish 3 statement types**:
   - Fact from data → no citation needed
   - Claim from literature → cite with DOI
   - Design decision → document with reasoning, clearly marked as a decision

6. **Analysis**: start with *"here is what the data shows"* BEFORE interpretation.

7. **Do NOT fabricate numbers, DOIs, dataset sizes, citations**. If not found → write *"citation needed"* or `[VERIFY: ...]`.

## Application to subagents

- Before proposing a new threshold/param → check the methods doc + literature.
- Before reporting results → clearly separate *observation* vs *interpretation*.
- Mark every unverified claim with `[VERIFY]` in the output.

## Escalation format (MANDATORY when applying Rule 0 or hitting an out-of-scope situation)

A curt "escalate user" reply is NOT acceptable. Use this format:

```
## ESCALATION
**Issue**: <1 sentence>
**Context**: <concrete evidence — file:line, log, manifest version>
**Options**: A. ...  B. ...  (C. ...)
**Recommendation**: <if grounded>
**Waiting on user**: <specific question>
```

While waiting for the user, the agent may prepare (dry-run, draft) but must NOT commit final changes.
