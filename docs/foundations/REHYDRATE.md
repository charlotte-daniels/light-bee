# REHYDRATE: flat bundle to working .claude/ layout

Every artifact here is a single file at one level, so nothing needs a folder upload. The original path is encoded in the filename with `__` as the separator. For example `skills__getting-started__SKILL.md` is really `skills/getting-started/SKILL.md`, and `agents__deploy-gate.md` is `agents/deploy-gate.md`.

You can read or edit any file here directly. To make Claude Code **use** them (skills, agents, hooks and the output style auto-load only from the nested layout), rehydrate once with the matching command, run from inside this flat folder. The full commands are in `START-HERE.md`; the summary is below.

## Naming map

- `START-HERE.md`, `REHYDRATE.md`, `AUDIT.md`, `COMPLETENESS.md`: reference docs, not copied into `.claude/`.
- `CLAUDE.md`: moves to the **project root** after rehydration (it is the project memory Claude Code reads).
- `plugin.json`: rehydrates to `.claude/.claude-plugin/plugin.json` (the manifest must live under `.claude-plugin/`; every other component sits at the plugin root).
- `settings.json`: rehydrates to `.claude/settings.json`.
- `hooks__*` becomes `hooks/*` (the two guardrail scripts).
- `skills__<name>__SKILL.md` becomes `skills/<name>/SKILL.md` (20 skills).
- `agents__<name>.md` becomes `agents/<name>.md` (4 reviewers).
- `output-styles__house-voice.md` becomes `output-styles/house-voice.md`.

## After rehydration

You get the standard tree:

```
<repo root>/
  CLAUDE.md                              project memory, fill the ${PLACEHOLDERS}
  .claude/
    .claude-plugin/plugin.json           plugin manifest
    settings.json                        permissions + hooks
    hooks/secret-scan.mjs                pre-write secret block
    hooks/format-gate.sh                 post-edit syntax gate
    output-styles/house-voice.md         the house voice
    agents/engineering-reviewer.md       binding gate
    agents/security-reviewer.md          binding gate
    agents/deploy-gate.md                binding gate
    agents/design-critic.md              advisory
    skills/<name>/SKILL.md               20 skills
```

Claude Code then reads the skills, agents, output style, hooks, and settings automatically. Commit the tree to your repository like any other source. Fill the `CLAUDE.md` placeholders (listed in `AUDIT.md`) before your first deploy.
