# AgentLedger Project Completion Spec
## Release Readiness, Operational Closure, and User Handoff

**Version:** 0.1
**Status:** Ready for Execution
**Author:** Codex
**Last Updated:** April 29, 2026
**Current Baseline:** `main` at `d47138b`
**Current Verification:** `pytest tests -q` -> `346 passed`
**Scope:** Final project closure after Layers 1-6 implementation

---

## Purpose

AgentLedger is implemented through Layer 6. The remaining work is not feature
implementation. It is release readiness: proving the system can be deployed from a
clean environment, confirming CI, closing documentation, making legal and security
decisions explicit, and tagging a coherent v0.1 release.

This spec defines exactly what must be done before the project can be considered
complete, who owns each task, and what acceptance evidence is required.

---

## Completion Definition

AgentLedger is complete when all of the following are true:

1. `main` is the single complete working branch and is pushed to origin.
2. A clean clone can build, migrate, test, and run the API.
3. GitHub CI passes on `origin/main`.
4. README, specs, completion summaries, process docs, and lesson docs reflect the
   final Layer 1-6 standing.
5. Security-sensitive routes, compliance exports, and PII handling have been
   reviewed and documented.
6. Operational runbooks exist for migrations, environment variables, backups,
   retention, and release rollback.
7. Legal scope is explicit: AgentLedger provides evidence infrastructure, not
   legal rulings, insurance underwriting, payment settlement, or regulated escrow.
8. A release tag, release notes, and branch cleanup decision are complete.

---

## Work Ownership Model

### Codex Can Do

Codex can perform implementation and verification tasks inside this repository:

- Inspect code, specs, docs, migrations, and tests.
- Add or update documentation files.
- Create completion summaries and runbooks.
- Add tests for missing behavior.
- Run local tests and report exact outputs.
- Run local Docker smoke tests if Docker is available.
- Run local migrations against configured development databases.
- Inspect Git status, branches, commits, and diffs.
- Commit and push changes when explicitly asked.
- Create tags and release notes when explicitly asked.
- Clean up remote branches only after explicit user approval.
- Search the codebase for auth, PII, retention, and compliance risks.
- Produce checklists for external legal, security, and infrastructure review.

### The User Must Do

The user must handle decisions and actions that require business judgment,
external access, legal authority, or production credentials:

- Confirm the release version, for example `v0.1.0`.
- Approve deletion or preservation of old local and remote branches.
- Provide access to GitHub Actions, repository settings, and deployment
  environments if Codex needs to inspect or change them.
- Configure production or staging secrets outside the repository.
- Approve any real deployment target and cloud infrastructure configuration.
- Decide backup retention, erasure retention, and audit log retention periods.
- Review legal disclaimers with qualified counsel.
- Decide whether AgentLedger is public demo software, private infrastructure,
  open source, or commercial product.
- Decide whether Layer 3 testnet deployment is required before v0.1 release.
- Approve privacy policy, terms of use, compliance wording, and liability scope.
- Approve any branch deletion, release publication, or production deployment.

---

## Phase 1: Repository and Branch Closure

### Goal

Ensure there is one complete branch representing the current product state.

### Codex Tasks

1. Run:

   ```bash
   git status -sb
   git branch --all
   git log --oneline --decorate -10
   ```

2. Confirm `main` matches `origin/main`.
3. Identify stale local and remote branches.
4. Identify any commits on non-main branches that are not reachable from `main`.
5. Produce a branch cleanup proposal.

### User Tasks

1. Confirm whether old branches should be deleted or preserved.
2. Approve any destructive cleanup commands, such as deleting local or remote
   branches.
3. Confirm whether `main` should be protected in GitHub branch settings.

### Acceptance Evidence

- `git status -sb` shows `## main...origin/main`.
- No uncommitted changes remain.
- Any retained branches have a documented reason.
- Any deleted branches were explicitly approved by the user.

---

## Phase 2: Fresh Deployment Smoke Test

### Goal

Prove the project can start from a clean environment.

### Codex Tasks

1. Verify dependencies install from the declared files:

   ```bash
   python -m pip install -r requirements.txt
   ```

2. Start the local stack:

   ```bash
   docker compose up -d --build
   ```

3. Run migrations from zero against a fresh database.
4. Run an upgrade migration against an existing development database, if one is
   available and approved.
5. Verify core local endpoints:

   ```bash
   GET /v1/health
   GET /docs
   GET /v1/ontology
   ```

6. Run one representative smoke path per layer:

   - Layer 1: submit or read a service manifest.
   - Layer 2: read issuer DID document or register a test agent.
   - Layer 3: read chain status or trust records.
   - Layer 4: create or read a context profile.
   - Layer 5: create or list workflows.
   - Layer 6: read a liability snapshot or file a test claim against seeded data.

### User Tasks

1. Confirm whether Codex may run Docker locally.
2. Provide or approve local database and Redis configuration.
3. Provide test-only private key material if full Layer 2 issuance should be
   smoke tested.
4. Decide whether Layer 3 blockchain smoke testing should use local mode,
   Polygon Amoy, or be deferred.

### Acceptance Evidence

- Docker services are healthy.
- Migrations complete without manual edits.
- `/v1/health` returns success.
- OpenAPI docs render.
- One smoke action per layer succeeds.
- Any skipped external dependency is documented with reason and owner.

---

## Phase 3: CI Verification

### Goal

Confirm the remote repository passes tests outside the local workstation.

### Codex Tasks

1. Inspect GitHub Actions workflow files.
2. Confirm current CI covers at least:

   - dependency install
   - migrations or schema checks
   - `pytest tests -q`
   - lint or formatting checks if configured

3. Inspect the latest workflow run for `origin/main`.
4. If CI fails, read logs and propose or implement a fix.

### User Tasks

1. Provide GitHub Actions access if needed.
2. Configure required repository secrets.
3. Approve any changes to branch protection rules.
4. Decide whether failing optional checks should block release.

### Acceptance Evidence

- Latest CI run for `origin/main` is green.
- Required checks are documented.
- Any deliberately skipped CI scope is documented.

---

## Phase 4: Documentation Closure

### Goal

Make the repository understandable to a new engineer, auditor, or reviewer.

### Codex Tasks

1. Create `spec/LAYER6_COMPLETION.md`.
2. Confirm `README.md` lists:

   - Layers 1-6 as complete
   - current test count
   - Layer 6 API surface
   - local start instructions
   - important environment variables

3. Update `AgentLedger_ProcessDoc.md` with:

   - Layer 6 completion summary
   - key decisions
   - remaining external decisions
   - final verification output

4. If `NORTHSTAR.md` is intended to exist, either:

   - update it, if the user provides it, or
   - create it from the roadmap context, if the user approves.

5. Update lesson indexes if Lessons 41-49 are not indexed.
6. Generate an endpoint inventory from the FastAPI app and compare it to README.
7. Add a release notes draft for `v0.1.0`.

### User Tasks

1. Review all public-facing claims for accuracy.
2. Approve product positioning language.
3. Decide whether `NORTHSTAR.md` should be created if absent.
4. Confirm whether docs should describe Layer 3 as code-complete, deployed, or
   blocked on testnet deployment.
5. Approve final release notes.

### Acceptance Evidence

- `spec/LAYER6_COMPLETION.md` exists.
- README matches actual API surface and test count.
- Process document has Layer 6 final entry.
- Lesson index includes all current lessons.
- Release notes draft exists.

---

## Phase 5: Security and Legal Readiness Review

### Goal

Reduce preventable security, privacy, and legal positioning risk before release.

### Codex Tasks

1. Review protected endpoints for API key or admin key gating.
2. Check that admin-only routes are not exposed behind normal API keys.
3. Search for accidental plaintext secrets or private keys in the repository.
4. Review compliance exports for field values or PII leakage.
5. Confirm Layer 4 erased records stay tombstoned and do not leak field metadata.
6. Confirm Layer 6 erased disclosure evidence uses empty raw data where required.
7. Confirm Redis failure behavior is documented:

   - claim filing rate limit currently fails open on Redis errors
   - claim status cache failures do not block claim transitions

8. Confirm session assertion verification is not still using a Phase 3 stub.
9. Add tests where a security-sensitive guarantee is not covered.
10. Draft a v0.1 legal scope note:

    - evidence infrastructure only
    - no binding legal rulings
    - no licensed insurance product
    - no payment settlement
    - no regulated escrow

### User Tasks

1. Review legal disclaimers with qualified counsel.
2. Decide whether Redis rate-limit failure should fail open or fail closed.
3. Decide retention policy for liability snapshots, claims, evidence, and exports.
4. Decide whether public deployments need a privacy policy before release.
5. Approve any claim that references HIPAA, SEC, EU AI Act, insurance, escrow, or
   legal liability.
6. Decide whether an external security review is required before release.

### Acceptance Evidence

- Auth gate review is documented.
- No known secret material is committed.
- PII leakage review is documented.
- Legal scope note is approved by the user.
- Any known security tradeoff has an owner and release decision.

---

## Phase 6: Operational Readiness

### Goal

Make AgentLedger operable after release.

### Codex Tasks

1. Create an operations runbook covering:

   - local startup
   - migration execution
   - rollback expectations
   - test commands
   - load test commands
   - common failure modes

2. Create an environment variable matrix covering:

   - required variables
   - optional variables
   - local defaults
   - production expectations
   - secrets that must never be committed

3. Document backup and retention considerations for:

   - PostgreSQL
   - Redis cache
   - liability snapshots
   - liability evidence
   - compliance export logs

4. Document observability needs:

   - API request logs
   - claim filing rate-limit events
   - Layer 6 claim lifecycle events
   - failed PDF export attempts
   - failed snapshot creation

5. Confirm migrations are ordered and named correctly.

### User Tasks

1. Choose deployment target.
2. Choose backup provider and retention periods.
3. Decide RPO and RTO targets.
4. Decide who receives operational alerts.
5. Provide production secrets through a secure secret manager.
6. Decide whether Redis is required for production startup or optional.

### Acceptance Evidence

- Operations runbook exists.
- Environment matrix exists.
- Backup and retention policy is documented.
- Migration order is verified.
- Production secret handling is defined.

---

## Phase 7: Release Hygiene

### Goal

Mark a clean, reviewable release point.

### Codex Tasks

1. Confirm the working tree is clean.
2. Confirm local `main` equals `origin/main`.
3. Run final verification:

   ```bash
   pytest tests -q
   git diff --check
   ```

4. Create a release tag if the user approves:

   ```bash
   git tag -a v0.1.0 -m "AgentLedger v0.1.0"
   git push origin v0.1.0
   ```

5. Draft release notes.
6. Generate a final branch cleanup plan.

### User Tasks

1. Approve release version.
2. Approve tag creation.
3. Approve release notes.
4. Approve remote branch deletion if desired.
5. Decide whether to create a GitHub Release from the tag.

### Acceptance Evidence

- Final tests pass.
- `git diff --check` passes.
- Release tag exists on origin.
- Release notes are published or stored in repo.
- Branch cleanup is complete or explicitly deferred.

---

## Recommended Execution Order

1. Documentation closure.
2. Security and legal readiness review.
3. Fresh deployment smoke test.
4. CI verification.
5. Operational readiness.
6. Release hygiene and tag.
7. Branch cleanup.

This order keeps low-risk documentation and review work ahead of deployment and
release actions. It also makes sure any security or legal changes are captured
before a final tag is created.

---

## Codex Execution Checklist

Codex should execute these tasks when asked to complete the project:

1. Create `spec/LAYER6_COMPLETION.md`.
2. Update `AgentLedger_ProcessDoc.md`.
3. Inspect whether `NORTHSTAR.md` should be created.
4. Update lesson index for Lessons 41-49.
5. Generate endpoint inventory and compare with README.
6. Review route auth gates and PII export paths.
7. Document Redis failure policy and ask user to confirm fail-open versus
   fail-closed for production.
8. Create operations runbook.
9. Create environment variable matrix.
10. Run `pytest tests -q`.
11. Run `git diff --check`.
12. Run Docker smoke test if user approves.
13. Inspect CI if GitHub access is available.
14. Prepare release notes.
15. Ask user to approve release version and tag.

---

## User Decision Checklist

The user must answer these before final release:

1. What release version should be used?
2. Should Layer 3 be released as code-complete only, or must testnet deployment be
   completed first?
3. Should Redis rate-limit failure fail open or fail closed in production?
4. What are the retention periods for snapshots, evidence, claims, disclosures,
   and compliance export logs?
5. Should `NORTHSTAR.md` be created if it is absent?
6. Which stale branches should be deleted?
7. Is a legal review required before public release?
8. Is an external security review required before public release?
9. What deployment target should be documented as canonical?
10. Should Codex create and push the release tag?

---

## Out of Scope for v0.1 Completion

The following should not block v0.1 unless the user explicitly changes scope:

- Licensed insurance underwriting.
- Binding dispute adjudication.
- Payment settlement.
- Production smart contract escrow.
- Governance organization formation.
- Multi-tenant enterprise administration.
- External SOC 2, HIPAA, or ISO certification.
- Production cloud deployment, unless the user defines deployment as part of
  v0.1 release.

---

## Final Completion Record Template

When all phases are complete, create a final record with this shape:

```markdown
# AgentLedger v0.1 Completion Record

**Release Tag:** v0.1.0
**Commit:** <sha>
**Date:** <date>
**Tests:** pytest tests -q -> <count> passed
**CI:** <link or status>
**Docker Smoke:** <status>
**Docs:** complete
**Security Review:** <status>
**Legal Scope Note:** approved
**Branch State:** main only / retained branches documented

## Known Deferred Items

- <item>

## User-Approved Release Decisions

- <decision>
```
