# Step 0 — Platform compatibility test

Run before any build work to prove the preview platform pieces fit together. Re-run the
checks marked ♻ one week before each delivery (preview API churn).

**Environment (Oct 2026):** azd 1.35.0 · `azure.ai.agents` 1.0.0-beta.18 · Terraform 1.11.4
(AzAPI provider) · Azure CLI 2.84.0 · gh 2.101.0 · uv 0.12.23 · CPython 3.14.8 ·
azure-ai-projects 2.7.0 · agent-framework-core 1.19.0 / -foundry 1.13.1 /
-foundry-hosting 1.0.0b260918.

| # | Check | Result | Evidence / notes |
|---|---|---|---|
| 1 | Local tools signed in to the target subscription | ✅ Pass | Versions above. `az`/`azd` login must be **tenant-pinned**; scripts use tenant-pinned credentials (`scripts/foundry_agents.py: make_credential`) and `--subscription` on `az` calls so a different global `az` default can't redirect them. |
| 2 | Latest stable Python on the hosted-agent runtime | ✅ Pass | Python **3.14** (`runtime: python_3_14` in `azure.yaml`, `agent/.python-version`). 3.15 is not GA. Agent Framework, azure-ai-projects and the hosting adapter install and run on 3.14. |
| 3 | azd + Terraform scaffold | ✅ Pass (hand-authored) | `infra.provider: terraform`; modules hand-authored with **AzAPI** only (`infra/*.tf`) — Foundry account/project, model deployments, guardrail policy, AI Search, App Insights, App Service + Easy Auth, Azure Policy, CI identities + federated credentials. azd outputs flow to the hooks. |
| 4 | Code-deploy hosted agent | ✅ Pass | `azd deploy clinical-agent` (code deploy, `remote_build`) → new agent version; invoke via Responses API. |
| 5 | Model quota | ✅ Pass (with deviation) | `gpt-5.4-mini` (200K TPM) and `gpt-5.4` judge (150K TPM) in **eastus2**. AI Search and App Service placed in **centralus** for capacity (`search_location`, `web_location`). No embedding model needed: the knowledge source uses semantic ranking over a keyword index. |
| 6 ♻ | Foundry IQ knowledge base via toolbox | ✅ Pass | `kb/provision_kb.py` (Search API `2026-05-01-preview`): index → knowledge source → KB (`extractiveData`, low reasoning effort) → toolbox (`kb/toolbox.yaml`). Agent identity needs **Search Index Data Reader** — granted by `scripts/postdeploy.ps1` because the identity only exists after first deploy. |
| 7 | Version-pinned evaluation | ✅ Pass (custom harness) | `evals/run_eval.py` invokes `name:version` directly, captures retrieved source ids + final answer from the agent's structured trailer, then runs Foundry cloud evaluators (groundedness, completeness, task adherence, content safety) on those rows. Chosen over `microsoft/ai-agent-evals` because the gate needs custom deterministic graders and retrieved-evidence mapping. |
| 8 | Version pinning / non-prod invoke | ✅ Pass | Endpoint selector pinned to one version; `scripts/foundry_agents.py pin --expect-current N` implements compare-and-swap. Candidate versions are invoked by version and receive no production traffic. |
| 9 ♻ | Continuous evaluation on live traffic | ⚠️ Fallback | Foundry **evaluation rules reject hosted agents** (`agent of kind 'hosted' not supported for evaluation rules`). Scheduled **trace** evaluations were tried next but runs stayed `in_progress` indefinitely and **blocked all other quality evaluations in the project** (CI gate failed closed); they are disabled (`continuous_eval.py --mode off`). Continuous evaluation = `scheduled-eval.yml` running the full suite against the pinned production version, plus production traces with `clinical.*` attributes on the dashboard. |
| 10 | RBAC split (create version vs move production) | ⚠️ Limitation | Both are agent write operations; no built-in role separates them. Compensating controls: promote identity federated **only** to `environment:production` (required reviewer, `main` only), compare-and-swap pin, CODEOWNERS on `.github/`, `infra/`, `evals/thresholds.yaml`, fork PRs get no credentials. |

## Operational findings

- **Concurrent version creates return 409.** `create-version` retries 409 with backoff;
  release and reset share the `agent-deploy` concurrency group, PR runs are serialized per PR.
- **Parallel evals interfere.** Running several 40-row evals against the agent at once
  produced failed/empty responses (throttling). Run one eval at a time, concurrency 3;
  `run_eval.py` retries each row up to 3× and records the service error.
- **Transient DNS/connection errors** on client networks: connection retries in
  `foundry_agents._request`, Search fetches and the OpenAI client (`max_retries=8`).
- **Judge strictness on refusals.** A model that correctly declines a red-team request but
  quotes the injected text ("APPROVED BY FDA") must not be scored as a defect; the safety
  grader ignores declining sentences.
- **Eval calibration.** Baseline: facts recall ≈ 0.97, groundedness mean ≈ 4.7, 0 safety
  defects. The "concise" break prompt drops to ≈ 0.85 facts recall (14/30 answers miss a key
  fact; mean 66 vs 201 words) → gate threshold 0.90. The LLM completeness judge is lenient
  on short answers, so the deterministic required-facts grader is the decisive metric.
- **Fail-closed proven** in unit tests (`tests/unit/test_gate.py`): missing metric, error
  rate, row mismatch and partial results all fail.
- **Guardrail blocks inside a hosted agent:** a Prompt Shields / content-filter rejection surfaces in Agent Framework as `ChatClientContentFilterException`. Unhandled, the hosted agent answered "insufficient evidence", hiding the guardrail. The middleware now converts it into an explicit refusal (`refusal_reason: guardrail_content_filter`, span attribute `clinical.guardrail_blocked`).
- **Eval queue hygiene:** if cloud evals hang, list runs with `evals.list` / `evals.runs.list` and cancel stuck `in_progress` runs; the gate fails closed on missing cloud metrics by design.
