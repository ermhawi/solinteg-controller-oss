<!-- BEGIN:nextjs-agent-rules -->
# This is NOT the Next.js you know

This version has breaking changes — APIs, conventions, and file structure may all differ from your training data. Read the relevant guide in `node_modules/next/dist/docs/` before writing any code. Heed deprecation notices.
<!-- END:nextjs-agent-rules -->

## Documentation standard v1.3.0

Follow [engineering-standards v1.3.0](https://github.com/ermhawi/engineering-standards/blob/v1.3.0/docs/policy.md), including mandatory [documentation preflight](https://github.com/ermhawi/engineering-standards/blob/v1.3.0/docs/documentation-hygiene.md), [Git hygiene and Definition of Done](https://github.com/ermhawi/engineering-standards/blob/v1.3.0/docs/git-hygiene.md) and [artifact cleanup/archival](https://github.com/ermhawi/engineering-standards/blob/v1.3.0/docs/artifact-hygiene.md).

**Before code or configuration changes:** read `docs/index.md`, relevant system/component architecture and design, requirements/ADRs, documented known issues and limitations, and relevant plans with their implementation status. Reconcile claims with current code, configuration, tests and, when appropriate, observed runtime. Report inconsistencies, unverified claims, existing blockers and affected documents in the task/plan *before* editing. Plans and memory are not evidence of deployment; a prepared broker is not a deployed broker.

**Before Definition of Done:** update impacted architecture, feature, operations, known-issues and plan-status documents in the same branch as the change, using actual code/test/runtime evidence. Record major decisions as ADRs, distinguish proposed from implemented and deployed functionality, and explain when no documentation update is needed. Never publish private installation data or internal docs without verified access control.

## Tool-call diagnostics

This applies to all MCP tools. If a tool attempt appears to stop before the MCP server records metrics, do not retry the same raw payload or route it through another tool.

Use `mcpsh_call_diagnostic` to record metadata only: attempted tool, operation/data class, argument hash if already known, observed symptom and whether a matching metrics entry exists. Set `not_received_by_server=true` only if the server has no matching metrics entry. Do not include raw arguments, file contents or secrets.

Before retrying, check `mcpsh_metrics_summary` or `mcpsh_recent_diagnostics` when relevant; use `mcpsh_tool_schema` for schema mismatches. Narrow inputs and prefer the smallest structured alternative over repeating a blocked call.

## Branch ownership and mandatory Git DoD

`Sundsvall` is a **permanent independent development branch** in the public `ermhawi/solinteg-controller-oss` fork. It tracks **only `origin/Sundsvall`**. **Never merge `Sundsvall` into `main`, `upstream/main` or the original `hspolander` repository.** Reviewed, sanitized commits are published directly to `origin/Sundsvall` only; never force-push.

Before new work, resolve old staged/untracked/ignored files, stashes, unpublished commits and stale auxiliary worktrees. A clean commit is not done until checks pass, the allowed tracking branch is in sync, and only the primary checkout remains. Preserve unknown work and private configuration instead of deleting or publishing it. Run `python3 /home/hakan/dev/github.com/ermhawi/engineering-standards/scripts/git_hygiene.py --mode preflight .` and `--mode done .` when available.

## Bitwarden Secrets Manager

För framtida arkivnycklar, API-tokens och hemligheter, följ [den gemensamma
Bitwarden Secrets Manager-runbooken](https://github.com/ermhawi/engineering-standards/blob/main/docs/operations/bitwarden-secrets-manager.md).
HA-Kube har befintlig Secrets Manager-/ESO-integration. Välj rätt projekt
och behörig maskinidentitet, använd Bitwarden UI eller skyddad SDK-process
för att skriva/läsa **värden**, verifiera readback och återställning utan
att skriva ut hemlighetsmaterial. Använd aldrig `bws-get` eller
`bws-create` som säkert agentflöde för känsliga värden.

## Mandatory artifact cleanup DoD

Before concluding any Solinteg task, inventory **agent-owned temporary resources outside Git** (systemd services/timers/drop-ins, active processes, test scripts, staging/release directories, containers, logs and snapshots). Record the owner, terminal state and rollback for every one-off physical trial. Verify the original dispatcher, poller, EV/phase safety guards and real telemetry BEFORE removing anything. Preserve terminal root status tombstones, required backups and the last working rollback.

Archive private evidence off Git with verified encryption, hashes, mounted-media identity and restore check; follow [the Solinteg archive and retired-artifacts runbook](docs/solinteg-archive-and-retired-artifacts-2026-11-01.md). Do not delete the only recovery copy if the decryption key has not been safely backed up outside the host. Remove only **owned, confirmed inactive** test unit files/scripts after archival and run `systemctl daemon-reload` when required. Unknown, older or operator-owned assets need separate explicit approval; report them as blockers instead of deleting them. **No unattended destructive cleanup timer** is authorized. No cleanup may issue inverter/EVCC commands or bypass a physical safety stop.

Report created, removed, preserved and blocked artifacts alongside Git hygiene. A clean Git status alone is insufficient DoD.

**Public-source safety:** Never commit the installation's actual private IP addresses, original invoices, billed consumption, tokens, or `.env` files. The public service files and env templates use synthetic private-address examples; replace them only in private deployment copies, not Git. No automatic synchronization of this checkout into the armed Solinteg installation.
