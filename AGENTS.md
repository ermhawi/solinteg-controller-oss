<!-- BEGIN:nextjs-agent-rules -->
# This is NOT the Next.js you know

This version has breaking changes — APIs, conventions, and file structure may all differ from your training data. Read the relevant guide in `node_modules/next/dist/docs/` before writing any code. Heed deprecation notices.
<!-- END:nextjs-agent-rules -->

## Documentation standard v1.2.0

Follow [engineering-standards v1.2.0](https://github.com/ermhawi/engineering-standards/blob/v1.2.0/docs/policy.md), including [Git hygiene and Definition of Done](https://github.com/ermhawi/engineering-standards/blob/v1.2.0/docs/git-hygiene.md). Before relevant code changes read `docs/index.md`; update affected feature, architecture and operations documentation together with code and record major decisions as ADRs. Distinguish proposed and implemented behavior; report why no documentation change was needed when applicable. Never publish internal docs without verified access control.

## Branch ownership and mandatory Git DoD

`Sundsvall` is a **permanent independent development branch** in the public `ermhawi/solinteg-controller-oss` fork. It tracks **only `origin/Sundsvall`**. **Never merge `Sundsvall` into `main`, `upstream/main` or the original `hspolander` repository.** Reviewed, sanitized commits are published directly to `origin/Sundsvall` only; never force-push.

Before new work, resolve old staged/untracked/ignored files, stashes, unpublished commits and stale auxiliary worktrees. A clean commit is not done until checks pass, the allowed tracking branch is in sync, and only the primary checkout remains. Preserve unknown work and private configuration instead of deleting or publishing it. Run `python3 /home/hakan/dev/github.com/ermhawi/engineering-standards/scripts/git_hygiene.py --mode preflight .` and `--mode done .` when available.

**Public-source safety:** Never commit the installation's actual private IP addresses, original invoices, billed consumption, tokens, or `.env` files. The public service files and env templates use synthetic private-address examples; replace them only in private deployment copies, not Git. No automatic synchronization of this checkout into the armed Solinteg installation.
