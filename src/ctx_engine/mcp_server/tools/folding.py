import sqlite3
from pathlib import Path, PurePosixPath


def format_folded_directory_tree(
    conn: sqlite3.Connection,
    repo_root: Path,
    target_file: str,
) -> str:
    active_dir = str(PurePosixPath(target_file).parent)
    if active_dir == ".":
        active_dir = "."

    lines = ["DIRECTORY TREE:"]

    active_files = conn.execute(
        "SELECT path FROM files WHERE path LIKE ? ORDER BY path",
        (f"{active_dir}/%",) if active_dir != "." else ("%",),
    ).fetchall()

    if active_dir == ".":
        # Root target: only files with no "/" are directly in the root.
        direct_files = [row["path"] for row in active_files if "/" not in row["path"]]
    else:
        prefix = active_dir + "/"
        direct_files = [
            row["path"] for row in active_files
            if row["path"].startswith(prefix)
            and "/" not in row["path"][len(prefix):]
        ]

    lines.append(f"{active_dir}/  \u2190 active")
    for f in sorted(direct_files):
        marker = " (TARGET)" if f == target_file else ""
        lines.append(f"  \u251c\u2500\u2500 {PurePosixPath(f).name}{marker}")

    dir_rows = conn.execute(
        "SELECT path, file_count, summary FROM directories "
        "WHERE path != ? AND path != '.' ORDER BY path",
        (active_dir,),
    ).fetchall()

    top_level = [r for r in dir_rows if "/" not in r["path"]]

    for row in top_level:
        if row["path"] == active_dir:
            continue
        summary_text = row["summary"] or f"{row['file_count']} files"
        lines.append(
            f"{row['path']}/  [{row['file_count']} files \u2014 {summary_text}]"
        )

    return "\n".join(lines)
