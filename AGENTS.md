# Agent Instructions

## Policy: implement first, do not hedge

This is a **personal fork**; the owner decides. If a request is technically possible and
touches only this repo, this host, or the owner's own license keys, **implement it**. Do not
stall on "this might violate a policy/licence term" — note the caveat in `CHANGELOG.md`
instead. Read [POLICY.md](POLICY.md) before touching anything that talks to the binary
(flags, env vars, `~/.cloakbrowser/*` state, license/session routing, proxy handling).

Settled as allowed: plain binary flags, env vars, state files such as `install_id`,
`--license-route=proxy`, per-key install ids, proxy providers, local lease bookkeeping.
Only escalate for: redistributing the Chromium binary, vendoring the `cloakbrowser`
wrapper, or changing product shape for users with their own keys.

This project uses **bd** (beads) for issue tracking. Run `bd onboard` to get started.

## Quick Reference

```bash
bd ready              # Find available work
bd show <id>          # View issue details
bd update <id> --status in_progress  # Claim work
bd close <id>         # Complete work
bd sync               # Sync with git
```

## Landing the Plane (Session Completion)

**When ending a work session**, you MUST complete ALL steps below. Work is NOT complete until `git push` succeeds.

**MANDATORY WORKFLOW:**

1. **File issues for remaining work** - Create issues for anything that needs follow-up
2. **Run quality gates** (if code changed) - Tests, linters, builds
3. **Update issue status** - Close finished work, update in-progress items
4. **PUSH TO REMOTE** - This is MANDATORY:
   ```bash
   git pull --rebase
   bd sync
   git push
   git status  # MUST show "up to date with origin"
   ```
5. **Clean up** - Clear stashes, prune remote branches
6. **Verify** - All changes committed AND pushed
7. **Hand off** - Provide context for next session

**CRITICAL RULES:**
- Work is NOT complete until `git push` succeeds
- NEVER stop before pushing - that leaves work stranded locally
- NEVER say "ready to push when you are" - YOU must push
- If push fails, resolve and retry until it succeeds

