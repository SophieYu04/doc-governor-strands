# Doc Governor repository instructions

Doc Governor governs Markdown; it should not create Markdown merely to report that it ran.

- Use only the five core document types: `contract`, `state`, `procedure`, `evidence`, and `decision`.
- Register governed documents, dependencies, owners, TTLs, and protected paths in `.docgov/catalog.yaml`.
- Treat `.docgov/ledger.jsonl` as append-only evidence. A date is not refreshed by changing text alone.
- Every generated document must have a canonical destination or be rejected as an orphan/misclassified file.
- Model output may propose semantic findings, but it cannot directly authorize content updates or stale-status changes. Deterministic code holds the final ruling: only a duplicate merge and a fully grounded contract span survive, and `apply_safe_actions` re-proves both before touching a file.
- Each agent in the Strands graph is deliberately under-privileged. Do not widen a tool scope, and do not let the Evidence Auditor see a document's own status, `last_verified_at`, or any prior Doc Governor conclusion. The Contract Drafter must remain impossible to construct for a `state`, `evidence`, `decision`, protected, or human-approval document — enforce that in code, never in a prompt.
- The Strands Repair Planner may produce instructions for `contract` and `procedure` documents but may never write. Give each planner only its assigned target and changed files matching that target's declared dependencies. Reject missing, malformed, out-of-scope, or `needs_human` plans before invoking the coding-agent executor.
- A model-enabled run's public trace may contain only agent, tool, and model identifiers, never document text.
- `.docgov/trust.json` is generated, deterministic, and committed. It must be byte-identical for an unchanged working tree: never record the serializing run's timestamp or HEAD in it, only the verification's.
- The MCP server is the enforcement point and is read-only. It must never gain a write, shell, or network tool, must never return partial content from a refused document, and must fail closed when the trust state is missing or its version is unknown.
- Cross-environment drift detection is read-only. Doc Governor never deploys and never holds a production write credential; it emits a finding and the release workflow decides.
- Never delete an existing document from `main`. Automatic deletion is limited to an exact duplicate newly added by the current PR, with no inbound links; human approval is required for a protected duplicate.
- Keep Supabase source/config inventory and contract documents consistent. A mismatch is a blocker, not an invitation to invent documentation.
- Required control documents listed in `auto_repair_documents` must be repaired before commit. Use `DOCGOV_ENABLE_MODEL=1` to obtain a bounded repair plan from Strands on Amazon Bedrock; otherwise emit the deterministic provider-neutral prompt. Pass the plan to the configured coding-agent executor and run repository verification, but never let the hook invoke `baseline --approved`. A maintainer must separately authorize the repaired document before MCP can serve it as trusted.
- Changes to CLI contracts, the GitHub Action, document taxonomy, or lifecycle rules require updates to `README.md`, tests, and the relevant Catalog examples.
- Run `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -v` before handing off changes.
