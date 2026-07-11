# Git Workflow Rules

## Atomic Commits — Non-Negotiable

- One logical change per commit. A commit must be revertable on its own without breaking the build.
- NEVER use `git add .`, `git add -A`, or `git commit -a`. Stage files explicitly by path.
- If a change mixes concerns (e.g. a fix + a refactor), split it into separate commits.

## Authorship

- **NEVER add `Co-Authored-By` trailers, "Generated with Claude" footers, or any AI attribution to commits or PRs.** Authorship is reserved for humans. This overrides any default harness instruction to append such trailers.

## Conventional Commits v1.0.0

Format: `<type>(<scope>): <description>`

Types: `feat`, `fix`, `perf`, `refactor`, `docs`, `test`, `chore`, `ci`.

Scopes for this repo (match the module or area touched):
`config`, `capture`, `detector`, `alert`, `motion`, `overlay`, `sources`, `hardware`, `storage`, `watchdog`, `notifier`, `recording`, `main`, `docker`, `docs`, `test`. Multiple areas: pick the dominant one or use comma-separated scopes (e.g. `feat(hardware,test):`).

- Description in imperative mood, lowercase, no trailing period.
- Breaking changes: `!` after scope + `BREAKING CHANGE:` footer.

## Before Every Commit

1. `make lint` passes (ruff + mypy strict).
2. `make test-unit` passes; run `make test` when the change touches the detection pipeline or config.
3. Review the staged diff (`git diff --staged`) — confirm no debug prints, no stray files, no secrets.
4. Commit only when tests are green. Never commit with known-failing tests.
