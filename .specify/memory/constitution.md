<!--
Sync Impact Report
- Version change: template -> 1.0.0
- Modified principles:
  - Placeholder Principle 1 -> I. Backward Compatibility First
  - Placeholder Principle 2 -> II. Test Evidence Before Merge
  - Placeholder Principle 3 -> III. Type-Safe, Readable Implementations
  - Placeholder Principle 4 -> IV. Secure-By-Default Data and Credential Handling
  - Placeholder Principle 5 -> V. Operational Reliability and Observability
- Added sections:
  - Engineering Constraints
  - Development Workflow & Quality Gates
- Removed sections:
  - Placeholder Section 2
  - Placeholder Section 3
- Templates requiring updates:
  - ✅ updated: .specify/templates/plan-template.md
  - ✅ updated: .specify/templates/spec-template.md
  - ✅ updated: .specify/templates/tasks-template.md
  - ✅ reviewed (no change required): .cursor/commands/speckit.analyze.md
  - ✅ reviewed (no change required): .cursor/commands/speckit.checklist.md
  - ✅ reviewed (no change required): .cursor/commands/speckit.clarify.md
  - ✅ reviewed (no change required): .cursor/commands/speckit.constitution.md
  - ✅ reviewed (no change required): .cursor/commands/speckit.implement.md
  - ✅ reviewed (no change required): .cursor/commands/speckit.plan.md
  - ✅ reviewed (no change required): .cursor/commands/speckit.specify.md
  - ✅ reviewed (no change required): .cursor/commands/speckit.tasks.md
  - ✅ reviewed (no change required): .cursor/commands/speckit.taskstoissues.md
  - ✅ reviewed (no change required): CLAUDE.md
  - ✅ reviewed (no change required): ARCHITECTURE.md
  - ✅ reviewed (no change required): AGENTS.md
- Follow-up TODOs:
  - TODO(RATIFICATION_DATE): Original adoption date is unknown and must be backfilled.
-->
# LiteLLM Constitution

## Core Principles

### I. Backward Compatibility First
Changes to public SDK, proxy APIs, provider behavior, and config formats MUST preserve
existing behavior unless a breaking change is explicitly approved. Any intended breaking
change MUST include migration guidance, release-note language, and compatibility coverage
tests before merge. Rationale: LiteLLM serves many providers and user environments where
silent regressions are high impact.

### II. Test Evidence Before Merge
All functional changes MUST include or update automated tests at the closest appropriate
layer (unit, integration, translation, or proxy). Bug fixes MUST include a regression test
that fails before the fix and passes after it. Large changes MUST include a clear test plan
for affected pathways. Rationale: broad provider and deployment variability requires proof,
not assumptions.

### III. Type-Safe, Readable Implementations
Production code MUST preserve explicit typing, clear naming, and established repository
patterns. Imports MUST be module-level unless needed to avoid circular dependencies. New
logic MUST prefer small helper functions over dense inline comprehensions when complexity
obscures intent. Rationale: maintainability across a large contributor surface depends on
predictable, readable code structure.

### IV. Secure-By-Default Data and Credential Handling
Code handling tokens, API keys, OAuth credentials, user identifiers, or billing data MUST
minimize exposure and follow least-privilege behavior. Sensitive values MUST NOT be logged
or stored in unsafe client persistence. Input validation and access checks MUST be explicit
for externally reachable interfaces. Rationale: gateway deployments process high-value
credentials and traffic from multiple tenants.

### V. Operational Reliability and Observability
Behavior that impacts routing, costs, retries, auth, or external integrations MUST include
structured logs/telemetry or equivalent traceability so incidents can be diagnosed. New
background jobs, rate-limiting paths, and caching flows MUST define failure handling and
safe defaults. Rationale: reliable operation across providers requires fast diagnosis and
controlled degradation.

## Engineering Constraints

- Changes MUST align with established project tooling (Black, Ruff, MyPy, pytest, Prisma
  patterns where applicable).
- Proxy data access MUST use Prisma model APIs rather than raw SQL for mutable operations.
- New dependencies MUST be justified by feature need and operational cost.
- Performance-sensitive or high-frequency paths SHOULD include bounded memory and latency
  considerations in design notes.

## Development Workflow & Quality Gates

- Plans generated via Speckit MUST pass a constitution check before implementation begins.
- Specifications MUST define measurable acceptance outcomes and explicit edge cases for
  risk-bearing flows (security, reliability, data integrity).
- Task plans MUST map each requirement to implementable work and include testing tasks when
  behavior changes are introduced.
- Pull requests MUST describe compatibility impact, test evidence, and operational impact
  (logging/metrics, migrations, rollout notes) before approval.

## Governance

This constitution is the highest-priority process authority for repository planning and
implementation workflows. In case of conflict, this document supersedes local workflow
preferences.

Amendment procedure:
- Propose constitution changes in a dedicated update with rationale and impacted templates.
- Include a Sync Impact Report summarizing modified principles and propagation status.
- Obtain maintainer approval before the amended constitution is treated as active policy.

Versioning policy:
- MAJOR: backward-incompatible governance changes or principle removal/redefinition.
- MINOR: new principle/section or materially expanded mandatory guidance.
- PATCH: clarifications and non-semantic wording refinements.

Compliance review expectations:
- Every plan and PR review MUST include an explicit constitution compliance check.
- Exceptions MUST be documented with rationale, scope, and a follow-up remediation task.

**Version**: 1.0.0 | **Ratified**: TODO(RATIFICATION_DATE): original adoption date unknown | **Last Amended**: 2026-04-24
