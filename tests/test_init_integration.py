import sqlite3
import subprocess
from ctx_engine.commands import run_init

def test_init_command_integration(tmp_path):
    # 1. Initialize Git repository
    subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=tmp_path, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path, capture_output=True, check=True)

    # 2. Create sample files
    (tmp_path / "a.py").write_text("""
import b

class Calculator:
    def add(self, x, y):
        return x + y
        
    def subtract(self, x, y):
        return x - y
""", encoding="utf-8")

    (tmp_path / "b.py").write_text("""
def run():
    print("running")
""", encoding="utf-8")

    (tmp_path / ".gitignore").write_text("ignored.py\n.ctx/\n", encoding="utf-8")
    (tmp_path / "ignored.py").write_text("print('ignored')\n", encoding="utf-8")

    # Stage and commit
    subprocess.run(["git", "add", "a.py", "b.py", ".gitignore"], cwd=tmp_path, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "initial commit"], cwd=tmp_path, capture_output=True, check=True)

    # 3. Run init command
    run_init(tmp_path)

    # 4. Verify database and WAL mode
    db_file = tmp_path / ".ctx" / "index.db"
    assert db_file.exists()

    conn = sqlite3.connect(db_file)
    conn.row_factory = sqlite3.Row

    journal_mode = conn.execute("PRAGMA journal_mode;").fetchone()[0]
    assert journal_mode.lower() == "wal"

    # Verify file indexing filtering out ignored.py
    file_records = {row["path"]: row for row in conn.execute("SELECT * FROM files;").fetchall()}
    assert "ignored.py" not in file_records
    assert "a.py" in file_records
    assert "b.py" in file_records

    # Check function records
    funcs = {row["id"]: row for row in conn.execute("SELECT * FROM functions;").fetchall()}
    assert "a.py::Calculator.add" in funcs
    assert "a.py::Calculator.subtract" in funcs
    assert "b.py::run" in funcs

    # Save original hashes
    original_add_hash = funcs["a.py::Calculator.add"]["semantic_hash"]
    original_sub_hash = funcs["a.py::Calculator.subtract"]["semantic_hash"]

    # Verify directories count
    dirs = {row["path"]: row for row in conn.execute("SELECT * FROM directories;").fetchall()}
    assert "." in dirs
    assert dirs["."]["file_count"] == 3  # a.py, b.py, .gitignore

    # Save original counts
    original_func_count = conn.execute("SELECT count(*) FROM functions;").fetchone()[0]
    original_file_count = conn.execute("SELECT count(*) FROM files;").fetchone()[0]
    original_call_count = conn.execute("SELECT count(*) FROM call_graph;").fetchone()[0]
    conn.close()

    # 5. Run init again to check idempotency
    run_init(tmp_path)

    conn = sqlite3.connect(db_file)
    assert conn.execute("SELECT count(*) FROM files;").fetchone()[0] == original_file_count
    assert conn.execute("SELECT count(*) FROM functions;").fetchone()[0] == original_func_count
    assert conn.execute("SELECT count(*) FROM call_graph;").fetchone()[0] == original_call_count
    conn.close()

    # 6. Make a semantic change to one function and re-run init
    (tmp_path / "a.py").write_text("""
import b

class Calculator:
    def add(self, x, y):
        # Semantic change
        return x + y + 1
        
    def subtract(self, x, y):
        return x - y
""", encoding="utf-8")

    run_init(tmp_path)

    conn = sqlite3.connect(db_file)
    conn.row_factory = sqlite3.Row
    funcs_after = {row["id"]: row for row in conn.execute("SELECT * FROM functions;").fetchall()}
    conn.close()

    # Check hash changes
    assert funcs_after["a.py::Calculator.add"]["semantic_hash"] != original_add_hash
    assert funcs_after["a.py::Calculator.subtract"]["semantic_hash"] == original_sub_hash
