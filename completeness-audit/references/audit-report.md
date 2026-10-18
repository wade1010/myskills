# Audit report contract

## Coverage vocabulary

- `covered`: the applicable behavior is supported by sufficient source, test, or browser evidence.
- `confirmed-gap`: an applicable behavior is absent, unreachable, unsafe, or incorrect, with evidence.
- `manual-review`: available evidence cannot settle the claim, especially when rendering or interaction was not exercised.

Record rules excluded by `skip_when` or failed applicability in a separate exclusions section; do not force them into a coverage status.

## Severity and confidence

Use severity consistently:

- `critical`: security, authorization, destructive data loss, or workflow-wide correctness failure with immediate serious impact;
- `high`: blocks a primary job or makes a meaningful portion of reachable data incorrect or inaccessible;
- `medium`: materially degrades completion, recovery, understanding, or accessibility but has a viable workaround;
- `low`: bounded friction or inconsistency with limited impact.

Use confidence `high`, `medium`, or `low` based on the directness and completeness of evidence. Severity describes impact; confidence describes certainty. A low-confidence claim remains `manual-review`, not `confirmed-gap`.

## Finding schema

Every `confirmed-gap` and `manual-review` finding must contain:

| Field | Required content |
|---|---|
| ID | Stable report-local identifier |
| Rule | Capability-catalog rule ID |
| Coverage | `confirmed-gap` or `manual-review` |
| Severity | `critical`, `high`, `medium`, or `low` |
| Confidence | `high`, `medium`, or `low` |
| Authority | `standard`, `official-pattern`, or `project-heuristic` |
| Evidence | `path:line` citations and, when used, browser route/viewport/actions/observation |
| Impact | Concrete affected actor and job |
| Recommendation | Smallest outcome-level correction, without implementing it |
| Verification | Test, source trace, or browser scenario that would prove resolution |

Do not promote a design preference to a standard. When multiple rules describe one root defect, group them into one finding and list all rule IDs rather than padding the count.

Reuse an exact canonical recurring finding ID from the evidence model whenever its definition matches. Otherwise use a deterministic semantic finding ID in the form `F-<AREA>-<DEFECT>` with uppercase kebab-case words. Reuse one ID for the same root defect across the report; do not use arbitrary sequence numbers.

## Report structure

1. **Scope and evidence inspected** - roles, routes, modules, data boundary, tests, browser use, and audit limitations.
2. **Executive result** - counts by coverage and severity; name the highest-risk outcome.
3. **Coverage table** - applicable capability, rule IDs, status, and concise evidence.
4. **Findings** - ordered by severity, then confidence and workflow impact.
5. **Applicability exclusions** - skipped capabilities and the conditions that exclude them.
6. **Manual verification queue** - exact scenarios needed to resolve every `manual-review`.
7. **Optional build handoff** - finding IDs only. Mark it unapproved until the user explicitly selects findings and asks to fix them.

A confirmed handoff should contain selected finding IDs, resulting `include` items, preserved `covered` behavior, explicit deferrals, and acceptance evidence. It is input to `$completeness-build` when available, not permission for this audit skill to edit code.
