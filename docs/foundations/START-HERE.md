# START HERE: Bluestaq Foundations

This is the **Bluestaq Foundations** baseline: a portable set of native Claude Code artifacts (skills, reviewer agents, a house voice, a `CLAUDE.md` conventions template, and guardrail hooks) that you drop into any new code project so it inherits a robust, App Store ready engineering standard from the first commit.

It is distilled and merged from two real, deployed Bluestaq systems: a single-file, offline-capable static web artifact, and a server-backed container application with a Large Language Model (LLM) integration. Both ship to the internal **Bluestaq App Store** (`*.apps.bluestaq.com`). This baseline carries the standing machinery of both, so a project of either shape is covered without you choosing the wrong template.

## The governing bar

A newcomer with zero prior context can go from an empty repository to a deployed, App Store ready application using only this bundle, by reading one file and following one skill. Everything below either gets you there or proves it gets you there.

## What is inside (flat now, foldered after one command)

Every file sits at one level here, so nothing needs a folder upload to GitHub or Claude Code. The real path is encoded in the filename with `__` as the separator, for example `skills__security-hardening__SKILL.md` is really `skills/security-hardening/SKILL.md`. Run the rehydrate step once and you get the standard tree.

- `CLAUDE.md`: the always-true conventions a project inherits. Fill the `${PLACEHOLDERS}` on first adoption (the list is in `AUDIT.md`).
- `skills__*__SKILL.md`: 20 skills. A cold-start trio (`getting-started`, `environment-setup`, `glossary`), one per engineering domain, and two user-invoked deploy runbooks (`release-and-deploy`, `app-store-deployment`).
- `agents__*.md`: four reviewers. Three binding gates (`engineering-reviewer`, `security-reviewer`, `deploy-gate`) that return `VERDICT: PASS` or `VERDICT: FAIL`, and one advisory `design-critic`.
- `output-styles__house-voice.md`: the Bluestaq house voice for every word a project emits.
- `settings.json`: pre-approved permissions, the App Store read-only tool allow-list, and two hooks (a pre-write secret scan that blocks a credential before it lands, and a post-edit syntax gate).
- `hooks__secret-scan.mjs`, `hooks__format-gate.sh`: the two guardrail scripts.
- `plugin.json`: the plugin manifest (rehydrates to `.claude-plugin/plugin.json`).
- `AUDIT.md`: the domain map, parameter table, provenance, and security findings.
- `COMPLETENESS.md`: the coverage proof.

## Install in two steps

### Step 1: rehydrate the flat files into a `.claude/` tree

Run one of these from inside this flat folder. Both expand the `__` separators into folders under `.claude/` at the root of your target repository, which is where Claude Code looks for skills, agents, the output style, hooks, and settings.

Bash (macOS, Linux, Windows Subsystem for Linux or Git Bash):

```bash
for f in *; do
  [ -f "$f" ] || continue
  case "$f" in START-HERE.md|REHYDRATE.md|AUDIT.md|COMPLETENESS.md) continue;; esac
  if [ "$f" = "plugin.json" ]; then t=".claude/.claude-plugin/plugin.json";
  else t=".claude/$(printf '%s' "$f" | sed 's#__#/#g')"; fi
  mkdir -p "$(dirname "$t")"
  cp "$f" "$t"
done
# CLAUDE.md belongs at the project root, not under .claude/
[ -f .claude/CLAUDE.md ] && mv .claude/CLAUDE.md ./CLAUDE.md
chmod +x .claude/hooks/*.sh .claude/hooks/*.mjs 2>/dev/null || true
echo "Rehydrated into .claude/  and  CLAUDE.md at root"
```

PowerShell (Windows, native):

```powershell
Get-ChildItem -File | Where-Object { $_.Name -notin 'START-HERE.md','REHYDRATE.md','AUDIT.md','COMPLETENESS.md' } | ForEach-Object {
  if ($_.Name -eq 'plugin.json') { $t = '.claude\.claude-plugin\plugin.json' }
  else { $t = Join-Path '.claude' ($_.Name -replace '__','\') }
  New-Item -ItemType Directory -Force -Path (Split-Path $t) | Out-Null
  Copy-Item $_.FullName $t -Force
}
if (Test-Path .claude\CLAUDE.md) { Move-Item .claude\CLAUDE.md .\CLAUDE.md -Force }
Write-Host "Rehydrated into .claude\  and  CLAUDE.md at root"
```

After rehydration you have the standard layout: `.claude/skills/<name>/SKILL.md`, `.claude/agents/<name>.md`, `.claude/output-styles/house-voice.md`, `.claude/settings.json`, `.claude/hooks/*`, `.claude/.claude-plugin/plugin.json`, and `CLAUDE.md` at the repository root. Claude Code reads all of these automatically. The files commit to your repository like any other source.

### Step 2: adopt and start

1. Open `CLAUDE.md` and replace every `${PLACEHOLDER}` with your project's real value, then delete the one-line adoption note at the top. The placeholder list, with how to obtain each value, is in `AUDIT.md` and in the `environment-setup` and `configuration-and-secrets` skills.
2. Start a Claude Code session and say: **"Follow the getting-started skill."** It threads every other skill in the right order, from an empty repository to a deployed App Store app.

## Two paths, one baseline (read this once)

Every skill states which **archetype** it applies to. Decide your archetype before you build, because it changes which deploy template the App Store detects:

- **Static or single-file** (offline HTML artifacts, dashboards, briefings, a built Single-Page Application served as static files). App Store template: `static-html` (or `node-react` if it has a build step). No server process, no port, no health endpoint. Strengths inherited: inline assets, locked Content Security Policy, no network egress, from-scratch rendering, an audit trail instead of telemetry.
- **Server-backed container** (an Application Programming Interface, a backend, an LLM scan, a shared synced workspace, a database). App Store template: `node-react`, `java-spring`, `python`, or `docker-only`. Strengths inherited: container-is-the-build, port 8080, non-root, health and readiness paths, fail-closed input validation, monotonic-revision sync, two-tier rate limiting, the SonarQube quality gate.

If you are unsure, the `getting-started` skill asks you the two questions that decide it.

## Skill levels: noob to expert

- **New to this:** read `getting-started`, then `glossary` whenever a word is unfamiliar. Every skill has a numbered Procedure and a Worked example you can copy.
- **Comfortable:** jump to the domain skill you need; each is self-contained with Decision rules and Failure modes.
- **Expert:** the Standards section of each skill is the checkable contract; the three gate agents enforce it. Read `AUDIT.md` and `COMPLETENESS.md` for the provenance and the coverage proof.

## House rules enforced throughout

UK English, no em-dashes, no `+` meaning "and", the `£`/`$`/`%` symbols, acronyms expanded on first use, never fabricated data, and nothing ships to the App Store without `deploy-gate` PASS plus an explicit human confirmation. The full voice is in `output-styles__house-voice.md`.
