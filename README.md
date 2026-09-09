# Doc Governor

AI coding agents treat repository documents as memory. When that memory is stale or unsupported, engineers waste work, tokens, and time—and changes become difficult to trust.

**Repair documents. Review evidence. Recheck every governed read.**

Doc Governor is a professional agent for software teams. It handles the repetitive work around human judgment: keeping source-backed documentation current, checking evidence, and preventing an agent from reading a document after its supporting source has changed.

## See it in five minutes

The public fixture is synthetic and needs no AWS credentials, private repository access, or database connection.

```sh
git clone https://github.com/SophieYu04/doc-governor-strands.git
cd doc-governor-strands
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev,mcp]'

# Repair, trust, and read the fixture through the real MCP stdio server.
python scripts/demo.py --mcp-stdio --keep

# Run the complete test suite.
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -v
```

The demo prints a JSON result and a temporary repository path. Review these outcomes:

| Demo event | What to look for |
| --- | --- |
| An Edge Function is added | Source-backed API and function inventories are repaired. |
| A new API note duplicates the canonical document | The eligible duplicate is removed and points to its canonical path. |
| A release date changes without evidence | The document is refused through the read path. |
| Protected public copy changes | The run returns `action_required`; the file is preserved for a person to review. |
| A dependency changes after a successful read | The next read refuses the document immediately, without another governance run. |
| Production evidence diverges from staging | `environment_drift` blocks affected environment documents. |

The decisive check is simple: a trusted document becomes unreadable as soon as one of its declared dependencies changes. The MCP recheck does this with hashes; it does not call a model or a network service.

## Three enforcement points

```text
BEFORE COMMIT                 PULL-REQUEST REVIEW             EVERY AGENT READ
source change + hashes        deterministic scan               get_document(path)
        │                     + optional Strands graph                 │
        ▼                              │                                ▼
Strands Repair Planner               ▼                         trust gate + live hashes
(Amazon Nova Lite)          deterministic ruling                ├─ pass: return bytes
        │                    + ledger + trust table              └─ fail: refuse body
        ▼
coding-agent executor
(Codex by default)
        │
        ▼
isolated repair → tests → independent read-only review → receipt
```

The local background worker handles the first path. It watches declared dependencies, batches changes during a quiet period, snapshots staged and working bytes in isolation, asks a read-only Strands Repair Planner for a bounded plan, and lets the configured coding agent edit only allowlisted documents. Tests, source/index integrity, policy versions, and a separate read-only coding-agent review must pass before a receipt and trust update are published. The worker never stages files; the commit integration stages only exact verified outputs.

The legacy-compatible command is also available:

```sh
docgov repair --json
```

## Why this fits Professional Agents

Software engineers and small teams repeatedly reconcile documentation with code, check whether “verified” claims still have evidence, and repeat tests after an agent handoff. Doc Governor clears that routine work while keeping consequential decisions with a person.

The system can repair a claim that source code proves. It routes unsupported, ambiguous, protected, legal, and public-copy claims to human review. Repair and trust are separate decisions.

## AWS Strands implementation

The implementation uses the Strands Agents SDK directly:

- `GraphBuilder` builds the Repair Planner and semantic governance workflows.
- Amazon Nova Lite (`us.amazon.nova-lite-v1:0`) is the default Bedrock model.
- Scoped `@tool` closures expose only the evidence and source paths assigned to each node.
- `BeforeToolCallEvent` hooks cancel disallowed or over-budget calls before invocation.
- Structured responses are validated by deterministic code; model failures fail closed.
- Public traces contain agent, tool, and model identifiers only.

The semantic governance graph has three constrained roles:

| Role | Scope |
| --- | --- |
| Evidence Auditor | Reviews one document's claims and recorded evidence. It cannot read source or write files. |
| Conflict Resolver | Compares only the conflicting documents and their declared source dependencies. It cannot write. |
| Contract Drafter | Proposes prose for an eligible `contract`; code prevents construction for `state`, `evidence`, `decision`, protected, or human-approval documents. |

The Repair Planner is separate from these roles. It returns a plan, never a write or approval. The executor and independent review run in isolated working copies. Deterministic validation rechecks paths, source/index integrity, citations, document hashes, dependency fingerprints, authorization versions, and receipt contents before publication.

## Every agent read goes through MCP

`docgov-mcp` is a read-only MCP server over stdio. Configure Codex, Claude Code, Cursor, or another MCP client:

```json
{
  "mcpServers": {
    "docgov": {
      "command": "docgov-mcp",
      "args": ["--root", "/absolute/path/to/your/repository"]
    }
  }
}
```

The server exposes read-only tools:

| Tool | Result |
| --- | --- |
| `get_document(path)` | Exact document bytes, or a refusal with a reason and source pointers when available. |
| `list_documents(type?, usable_only?)` | Documents that pass the same usability checks; refused entries can be requested with `usable_only=false`. |
| `document_status(path)` | Trust record and live fingerprint status, never document content. |
| `list_verifications` / `verification_status` | Reusable command evidence without executing a command. |

Refusals always return `content: null`. The server rejects traversal, absolute paths, URL schemes, non-Markdown paths, and symlinks escaping the repository. Missing or unknown trust-state versions fail closed. The MCP read path has no write, shell, or network tool and does not prevent a client from bypassing it with another filesystem tool.

Trust is evidence-bound, not proof of truth. The Catalog is owner-maintained, and every dependency must be declared. TTLs are checked when trust state is regenerated; the MCP read rechecks stored document and dependency hashes on every request.

## Use it in a repository

### Register documents and evidence

```sh
python -m pip install 'doc-governor[mcp]'
docgov init
```

Register canonical documents in `.docgov/catalog.yaml`. The five supported types are:

- `contract`: source-backed specification
- `state`: time-limited operational claim
- `procedure`: operating instructions
- `evidence`: immutable dated record
- `decision`: supersedable rationale

Each record declares its owner, dependencies, approval policy, and (when needed) TTL. `.docgov/ledger.jsonl` is append-only evidence; `.docgov/trust.json` is the deterministic table consumed by MCP.

### Run local repair

Install the background worker with a verification command and start it in the repository:

```sh
python -m pip install -e '.[bedrock,mcp]'
docgov install --verify-command 'python3 -m unittest discover -v'
```

The planner uses short-lived AWS credentials and Nova Lite. Codex is the default local executor; a trusted stdin-capable command can be configured for another coding agent. The executor never receives permission to change source code, Catalog policy, ledger history, or protected documents.

### Add pull-request governance

Copy `.github/workflows/docgov-review.yml` and configure a short-lived GitHub OIDC role with the least-privilege policy in [`infra/aws/bedrock-inference-policy.json`](infra/aws/bedrock-inference-policy.json). The action defaults to deterministic mode; enable Bedrock only for same-repository pull requests when `DOCGOV_AWS_ROLE_ARN` and `DOCGOV_ENABLE_MODEL=true` are configured.

```yaml
- uses: SophieYu04/doc-governor-strands@4242aa1f0a8ad956df855783866b8e3b82df1808
  with:
    mode: review
    base_sha: ${{ github.event.pull_request.base.sha }}
    head_sha: ${{ github.event.pull_request.head.sha }}
    apply: ${{ github.event.pull_request.head.repo.full_name == github.repository }}
    enable_model: ${{ vars.DOCGOV_ENABLE_MODEL == 'true' }}
    model_id: us.amazon.nova-lite-v1:0
```

Safe duplicate merges and grounded contract spans are re-proved by deterministic code. Ambiguous or protected changes produce a decision card and remain blocked until a maintainer authorizes them.

## Test the model path

The normal test suite needs no AWS credentials. `tests/test_strands_graph.py` drives the real Strands graph, nodes, scoped tools, structured output, and budget hooks with a test model in place of the Bedrock network call. This verifies SDK wiring and enforcement; it is not evidence of a live model response.

For the isolated live-model schema demonstration, use your own AWS profile:

```sh
AWS_PROFILE=docgov-local AWS_REGION=us-west-2 \
python scripts/background_demo.py --live --output /tmp/docgov-live-schema
```

The retained acceptance evidence under [`submission/`](submission/) records observed runs and sanitized hashes. It does not claim provider-level traces for every call or a production deployment.

## Read-only environment drift

The Supabase adapter fetches Security and Performance Advisor results through `GET` endpoints using `advisors_read`. It stores redacted immutable snapshots and never executes SQL, deploys functions, or changes a project.

```sh
SUPABASE_ACCESS_TOKEN=... docgov drift --collect --apply \
  --supabase-projects 'staging=PROJECT_REF,production=PROJECT_REF'
```

Drift findings affect release-readiness and can make documents describing the affected environment unreadable. Advisor output is a lint result set, so this detects symptomatic drift rather than every possible configuration change.

## Repository map

- [`docgov/repair_agents.py`](docgov/repair_agents.py): Strands Repair Planner
- [`docgov/background.py`](docgov/background.py): durable local worker
- [`docgov/repair_executor.py`](docgov/repair_executor.py): isolated executor and publication checks
- [`docgov/agents.py`](docgov/agents.py): semantic Strands graph
- [`docgov/coding_review.py`](docgov/coding_review.py): independent coding-agent review and receipts
- [`docgov/mcp_server.py`](docgov/mcp_server.py): read-only document supply
- [`docgov/trust_state.py`](docgov/trust_state.py): deterministic trust table
- [`examples/supabase-demo/`](examples/supabase-demo/): credential-free fixture
- [`.docgov/catalog.yaml`](.docgov/catalog.yaml): document registry and policies

## Limits

Doc Governor does not prove that prose is true, restrict other filesystem clients, deploy databases, or grant AWS credentials. Model output proposes; deterministic checks and owner authorization decide. A live Bedrock run must be recorded separately from mocked tests. The public fixture is the reproducible demonstration and contains no private application source or credentials.

Apache-2.0 · [LICENSE](LICENSE)
