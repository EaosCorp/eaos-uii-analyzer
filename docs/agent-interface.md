# Agent Interface — exposing the system to AI agents

> Agents are first-class actors on this platform, not an afterthought
> bolted onto a human UI. This doc records what the research says agents
> consume well, what this repo already provides, and the designed path for
> the rest. Companion: `architecture.md` §10, root `AGENTS.md`.

---

## 1. What agents actually consume well (research)

The 2025–2026 agent-tooling consensus converged on a few findings that map
almost perfectly onto Unix design sense:

* **CLIs beat bespoke REST clients.** Agents discover capabilities from
  `--help`, invoke non-interactively, parse structured output, retry on
  meaningful exit codes, and diagnose from stderr. The emerging contract:
  every command supports `--json`; stdout carries data, stderr carries
  errors; exit code 0 means stdout is trustworthy. Help text is the most
  important documentation surface an agent reads.
* **MCP (Model Context Protocol)** is the standard integration layer:
  **resources** (read-only, URI-addressed context: files, snapshots, query
  results) and **tools** (executable actions the agent chooses to call).
  The single most-used MCP server is the filesystem server — which says
  something important:
* **"Everything is a file" works on agents too.** Models are heavily
  trained on shells and file trees; browsable, greppable, predictable
  hierarchies (think `/proc`, `/sys`) let an agent orient itself with `ls`
  and `cat` instead of memorizing an API. Directory-shaped context (the
  `AGENTS.md` convention, skill folders) is now standard practice.

Sources: [Designing CLI tools for AI agents](https://archit15singh.github.io/posts/2026-02-28-designing-cli-tools-for-ai-agents/),
[the --json pattern](https://www.gibil.dev/blog/cli-json-pattern),
[why agents gravitate to CLIs](https://bytebridge.medium.com/why-ai-agents-gravitate-to-clis-over-rest-apis-999dfa113d9d),
[API + CLI + skills architecture](https://blog.wu-boy.com/2026/04/api-cli-skills-architecture-for-ai-agents-en/),
[MCP guide (tools vs resources)](https://dev.to/agdex_ai/mcp-tools-2026-the-complete-model-context-protocol-guide-for-ai-agents-3ib0).

The deeper reason this system is unusually agent-legible: **the evidence
log is already the ideal agent substrate.** One ordered, immutable,
self-describing stream where every record carries its own provenance. An
agent never has to reconcile five databases; it reads one log and walks
`causation_id` edges. Lineage is the agent's "why"; the capability manifest
is the agent's "what can I do here".

## 2. What exists today

| Surface | State | Notes |
|---|---|---|
| `uii` CLI | **built** | mirrors `/v1` one-to-one; `--json` on every verb; exit codes 0/2/3; stdlib, runs anywhere including on the hub over SSH |
| `/v1` API + SSE | **built** | one query surface (`/v1/evidence`) over everything; `/v1/capabilities` describes every module from its manifest; SSE with sequence-based resume |
| Evidence lineage | **built** | `uii lineage <id>` / `GET /v1/evidence/{id}/lineage` |
| Actor identity | **built** | every command carries `trace.actor` (`agent:eddy-om@site`, `user:…`, `system:scheduler`); audit envelopes on rejection and ack |
| **Authority enforcement** | **built** | actor class × declared risk × ingress path ceiling, in the command gateway; agents run `routine` alone, `disruptive` returns an approval id a human grants (`uii approve`); idempotency keys make retries safe |
| **Evidence export bundles** | **built** | `uii export -o bundle.tgz [--module --kind --since --correlation]`: manifest + evidence.jsonl + chain.json + a README written for the next reader (human or agent); contiguous slices re-verify offline |
| `AGENTS.md` | **built** | repo-root contract: how to run, test, query, and what an agent may and may not do |
| MCP server | optional, deferred | §4 — a mechanical wrapper over `/v1` whenever a shell-less surface needs one; deferring costs nothing *because* the CLI/API 1:1 rule holds |
| Filesystem projection | maybe-later | §5 — the export bundle covers the "hand an agent the record" need; a live FUSE tree only if field use asks for it |

An agent operating the bench today does it exactly like a person in a
terminal, which is the point:

```bash
uii --json modules                 # who is here, what state, what status
uii --json health                  # NE107 rollup + active alerts
uii --json cmd sample --module nh4mod-01 --watch
uii --json lineage 019fa1…        # why does this number look like this
uii --json evidence --kind identity --module nh4mod-01   # life story
```

## 3. Rules for agents (now ENFORCED, not advisory)

1. Commands go through the command gateway like every actor's; there is no
   agent side door. Manifest-declared `risk` gates what an agent runs
   alone: `routine` yes; `disruptive` returns `approval_required` + an
   approval id a human grants or denies (both audited); `hazardous` always
   needs a human, for everyone. Enforced in `uii/hub/authority.py` +
   `commands.py`.
2. Authority also binds to **connectivity**: each ingress path has an
   absolute risk ceiling (cellular/OT are capped at `routine`; approval
   cannot launder a request over a capped path). One `local` path is wired
   today; the seam is in place for the rest.
3. Agents read from evidence, never from module sockets. When an agent
   needs the record to make a call, `uii export` produces a bounded,
   self-describing, tamper-evident bundle designed to be dropped into
   context (its internal README explains itself to the reader).
4. Everything an agent does is attributable (`actor:`) and audited; agents
   retry with an `Idempotency-Key` so a retried command cannot run twice.

## 4. Designed: the MCP server (`uii-mcp`)

A thin adapter over `/v1` — deliberately no new capability, only projection:

* **Resources** (read-only):
  `uii://hub/system`, `uii://hub/capabilities`, `uii://modules/{id}`,
  `uii://modules/{id}/history`, `uii://observations/latest`,
  `uii://alerts/active`, `uii://evidence/{id}`,
  `uii://evidence/{id}/lineage`.
* **Tools** (actions, risk-gated): `submit_command(module, type, params)`,
  `ack_alert(rule, module)`, `release_module(id)`, `export_evidence(window)`.
* Tool descriptions are generated from the capability manifest, so a new
  module class (vision, rotating) exposes its commands to agents with zero
  MCP-server changes — same trick as the planned OT tag-map compiler.

## 5. Designed: the filesystem projection (`uii fs`)

A `/proc`-style read-only tree, because `ls`/`cat`/`grep` is the interface
agents (and field techs over SSH) already know:

```
/run/uii/
  system.json
  roles/nh4-influent/{config.json, occupied_by}
  modules/nh4mod-01/
    manifest.json  state  status        # OPERATIONAL · ok (NE107)
    latest/nh4.json                     # newest observation per channel
    calibration.json                    # active cal envelope
    alerts/                             # active alerts for this module
    history.jsonl                       # identity trail
  alerts/active.json
  evidence/tail.jsonl                   # last N envelopes, follow-able
```

Two implementation steps: (1) `uii fs export <dir>` snapshot — trivial,
pure API client, useful immediately for handing an agent "the plant as a
directory"; (2) a small FUSE mount for live reads. Writes never happen
through the filesystem; actions remain CLI/MCP/API so the gateway and audit
hold.

## 6. Roadmap order

1. ~~Risk/role enforcement + approval flow~~ **done** (authority.py; the
   real blocker for agent autonomy)
2. ~~Evidence export bundles as agent context~~ **done** (`uii export`)
3. Runbook skills on the hub: checklist files that teach any agent the
   investigation patterns (alert → lineage → cal history → propose)
4. Real per-path ingress stamping as the OT/cloud/cellular listeners land
5. `uii-mcp` wrapper only when a shell-less agent surface actually needs
   one (mechanical generation over `/v1`; nothing to design in advance)
6. Live filesystem projection (FUSE) only if field/agent use demands more
   than bundles
