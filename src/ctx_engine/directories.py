from collections import defaultdict
from pathlib import PurePosixPath

def build_directory_counts(tracked_paths: list[str]) -> dict[str, int]:
    """Calculate the file count for each directory including descendants, with root as '.'."""
    counts = defaultdict(int)
    for path in tracked_paths:
        parts = PurePosixPath(path).parts[:-1]
        counts["."] += 1
        prefix = ""
        for part in parts:
            prefix = f"{prefix}/{part}" if prefix else part
            counts[prefix] += 1
    counts.setdefault(".", 0)
    return dict(counts)
