# AgentLedger v0.1.1 Release Notes

**Release type:** Documentation, license, and release-hygiene update
**Base:** v0.1.0 local proof of concept

## Summary

v0.1.1 preserves the v0.1.0 proof-of-concept implementation and updates the repository metadata and educational materials so the public repository reflects the current state more accurately.

## Changes

- Added a root MIT `LICENSE` file.
- Updated README license, support, limitations, Node/npm, and open-source-status language.
- Added GitHub Actions CI for the Python test suite.
- Audited and improved Lessons 01-60 with beginner-friendly framing and clearer status/legal-scope language.
- Clarified Layer 3 local-mode versus live-chain behavior in the lessons.
- Clarified Layer 6 compliance/liability outputs as evidence infrastructure, not legal rulings or certifications.
- Updated architecture, dataset, installation, and monitoring docs to remove stale license and version TODOs.

## Validation

Local validation command:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest -p pytest_asyncio tests -q
```

Observed result:

```text
346 passed, 9 warnings in 763.13s
```

Note: the local shell wrapper timed out immediately after pytest printed the passing result. The pytest output itself reported all tests passing.

## Remaining Owner Actions

- Enable or confirm GitHub private vulnerability reporting.
- Configure branch protection to require CI before merging to `main`.
- Decide whether Layer 3 live Amoy deployment is required for a future non-POC release.
- Add production deployment architecture, monitoring, backups, and security/legal review before real user data or hosted production use.
