from pathlib import Path


def run_quickstart(repo_root: Path) -> None:
    is_git_repo = (repo_root / ".git").is_dir()
    repo_name = repo_root.name

    header = (
        f"  ctx -- Auto-Updating Codebase Context Engine"
    )
    subtitle = (
        f"  5-minute setup for: {repo_root}"
    )

    if not is_git_repo:
        subtitle += "  (note: this directory is not a git repo -- run 'git init' first)"

    width = max(len(header), len(subtitle)) + 4
    border = "  " + "─" * (width - 2)

    lines = [
        "ctx quickstart",
        "",
        f"  ┌{'─' * (width - 2)}┐",
        f"  │ {header}{' ' * (width - 4 - len(header))} │",
        f"  │ {subtitle}{' ' * (width - 4 - len(subtitle))} │",
        f"  └{'─' * (width - 2)}┘",
        "",
        "  Step 1: Build the initial index",
        "  " + "─" * (width - 2),
        "  $ ctx init",
        "  Parses all git-tracked files. Builds import + call graph.",
        "  Fast on re-runs (mtime cache skips unchanged files).",
        "",
        "  Step 2: Generate AI summaries",
        "  " + "─" * (width - 2),
        "  $ export ANTHROPIC_API_KEY=your-key-here",
        "  $ ctx summarize --dry-run   # preview scope and cost first",
        "  $ ctx summarize             # generate purpose/summary for all files/functions",
        "",
        "  Or to do both in one command:",
        "  $ ctx sync",
        "",
        "  Step 3: Install git hooks",
        "  " + "─" * (width - 2),
        "  $ ctx install-hooks",
        "  Adds pre-commit validation (blocks commits if index is stale)",
        "  and post-commit logging (records each commit in the change log).",
        "",
        "  Step 4: Connect to Claude Code",
        "  " + "─" * (width - 2),
        "  $ ctx generate-mcp-config",
        "  Writes .mcp.json so Claude Code launches ctx serve automatically.",
        "  Claude Code will then call ctx_get_context() before editing any file.",
        "",
        "  Step 5: Enable background monitoring (optional)",
        "  " + "─" * (width - 2),
        "  $ ctx watch --daemon [--with-ollama]",
        "  Detects file changes in real time. With --with-ollama, automatically",
        "  re-summarizes changed functions using a local Ollama model.",
        "  Works with any editor -- VS Code, Vim, JetBrains, etc.",
        "",
        "  Step 6: Add danger zones and decisions (optional but recommended)",
        "  " + "─" * (width - 2),
        "  $ ctx danger detect          # auto-detect from code heuristics",
        "  $ ctx danger add \"*\" \"...\" --reason \"...\"",
        "  $ ctx decision add \"...\" --reason \"...\"",
        "",
        "  Step 7: Export context files",
        "  " + "─" * (width - 2),
        "  $ ctx export",
        "  Generates CLAUDE.md, .github/copilot-instructions.md, .ctx/opencode.md.",
        "  These are auto-updated by 'ctx sync'. Never edit them manually.",
        "",
        "  " + border,
        "  Full reference:  ctx --help",
        "  Per-command:     ctx <command> --help",
        "  Index health:    ctx status",
    ]

    print("\n".join(lines))
