import subprocess
from pathlib import Path

# Map of file extensions to their corresponding tree-sitter language keys.
EXTENSION_TO_LANGUAGE: dict[str, str] = {
    ".py": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".ts": "typescript",
    ".mts": "typescript",
    ".cts": "typescript",
    ".tsx": "tsx",
    ".go": "go",
    ".rs": "rust",
}

def assert_inside_git_repo(repo_root: Path) -> None:
    """Check if the given directory is inside a Git repository work tree.

    Raises:
        ValueError: If the directory is not a Git repository.
    """
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=True
        )
        if result.stdout.strip() != "true":
            raise ValueError(f"Directory {repo_root} is not inside a git repository.")
    except (subprocess.CalledProcessError, FileNotFoundError) as err:
        raise ValueError(f"Directory {repo_root} is not inside a git repository.") from err

def discover_all_tracked_paths(repo_root: Path) -> list[str]:
    """Return all git-tracked and untracked-but-not-ignored file paths, relative to repo root.

    Raises:
        RuntimeError: If git ls-files fails.
    """
    try:
        result = subprocess.run(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=True
        )
        return [line.strip() for line in result.stdout.splitlines() if line.strip()]
    except subprocess.CalledProcessError as err:
        raise RuntimeError(f"Failed to list git files: {err.stderr}") from err

def discover_parseable_files(repo_root: Path) -> dict[str, str]:
    """Filter discover_all_tracked_paths by supported file extension.

    Returns:
        A dict mapping repo-relative paths to their tree-sitter language string.
    """
    all_paths = discover_all_tracked_paths(repo_root)
    parseable_map: dict[str, str] = {}
    for relative_path in all_paths:
        ext = Path(relative_path).suffix
        if ext in EXTENSION_TO_LANGUAGE:
            parseable_map[relative_path] = EXTENSION_TO_LANGUAGE[ext]
    return parseable_map


def get_parseable_extensions() -> set[str]:
    return set(EXTENSION_TO_LANGUAGE.keys())
