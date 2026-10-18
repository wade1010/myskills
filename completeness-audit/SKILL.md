---
name: completeness-audit
description: "Audit an existing operational or data-heavy web-app area for missing, unreachable, unsafe, or unverifiable product capabilities. Use when the user explicitly invokes $completeness-audit for an evidence-based review of an app, module, or feature; explicit invocation is required. Do not invoke when the request is for marketing, e-commerce, editorial, implementation, planning, styling critique, generic code review, or cross-module consistency comparison."
license: MIT
disable-model-invocation: true
---

# Completeness Audit

**Explicit invocation only.** Continue only when the user invoked this skill through the client's native explicit syntax, such as `$completeness-audit`, `/completeness-audit`, or a namespaced plugin command. Otherwise stop and explain that this broad read-only audit must be requested explicitly.

Audit existing behavior without editing application files.

## Boundaries

- Remain read-only. Do not fix findings, reformat files, generate migrations, or change tracked application state.
- Limit v0.1 to operational apps such as admin systems, CRMs, internal tools, and SaaS operations; do not audit marketing, e-commerce, or editorial product design with this catalog.
- Report in chat. Write a report file only when the user explicitly asks for one.
- Judge contextual applicability, not checklist volume. Do not flag a skipped capability without explaining why it applies.
- Source inspection can complete source-verifiable findings. Browser use is optional; rendered claims not actually exercised must be `manual-review`.

## Load the right guidance

Read [the capability catalog](references/capability-catalog.md), [the evidence model](references/evidence-model.md), and [the audit report contract](references/audit-report.md). If the target uses React, Next.js, shadcn/ui, TanStack Table, tRPC, or Prisma, also read [the React/Next audit reference](references/react-next.md).

## Workflow

1. **Fix the scope.** Name the app area, resource, roles, relevant routes, and data boundaries. For a broad request, partition the audit before collecting findings.
2. **Inspect before judging.** Trace UI entry points through queries, mutations, validation, authorization, and persistence. Record existing paths and project conventions.
3. **Use bounded delegation only when useful.** For a broad audit and when subagents are available, use 2-4 read-only subagents with non-overlapping module or evidence ownership. Give each the same finding schema. Prohibit nested delegation and integrate their evidence centrally. Otherwise audit in one agent.
4. **Assess applicable rules.** Apply catalog conditions and authority. Require positive evidence from the request, domain, or repository before a rule enters coverage; do not inventory every unmet trigger as an exclusion. For collections, verify:

   `displayed rows = paginate(sort(filter(complete reachable dataset)))`

5. **Classify evidence.** Use only `covered`, `confirmed-gap`, and `manual-review`. A `confirmed-gap` needs both applicability and evidence. Use `manual-review` when rendering or interaction must be observed and was not, or when available evidence cannot settle the claim. Do not omit an applicable rule merely because its rendered behavior was not exercised; put the unresolved claim in the manual-verification queue.
6. **Use the browser selectively.** When available and valuable, verify only unresolved interaction, layout, focus, responsive, or end-to-end claims. Record route, viewport, actions, and observation. Do not let a browser happy path stand in for server authorization or full-dataset correctness.
7. **Report, do not repair.** Follow the report contract, including exclusions. If the user wants fixes, let them select findings. Only after explicit confirmation may those selections become a pre-approved contract for `$completeness-build` when that skill is available.

## Exit conditions

Finish with evidence-backed coverage, prioritized findings, explicit applicability exclusions, and a named verification path for every `manual-review`. Do not mutate code or describe unverified behavior as fact.
