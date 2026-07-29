import subprocess
import sys
from pathlib import Path


def test_cli_imports():
    from ctx_engine import __version__
    assert __version__


def test_cli_help():
    result = subprocess.run(
        ["ctx", "--help"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0
    assert "ctx" in result.stdout


def test_cli_version():
    result = subprocess.run(
        [sys.executable, "-m", "ctx_engine.cli", "--version"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0


def test_pyproject_toml_exists():
    src_dir = Path(__file__).resolve().parent.parent
    pyproject = src_dir / "pyproject.toml"
    assert pyproject.exists()
    content = pyproject.read_text(encoding="utf-8")
    assert "[project]" in content
    assert "name = " in content
    assert "version = " in content


def test_hatch_build_config():
    src_dir = Path(__file__).resolve().parent.parent
    pyproject = src_dir / "pyproject.toml"
    content = pyproject.read_text(encoding="utf-8")
    assert "hatchling" in content
    assert "packages = " in content


def test_dev_optional_deps():
    src_dir = Path(__file__).resolve().parent.parent
    pyproject = src_dir / "pyproject.toml"
    content = pyproject.read_text(encoding="utf-8")
    assert "[project.optional-dependencies]" in content
    assert "dev" in content
    assert "dotenv" in content


def test_classifiers_present():
    src_dir = Path(__file__).resolve().parent.parent
    pyproject = src_dir / "pyproject.toml"
    content = pyproject.read_text(encoding="utf-8")
    assert "classifiers" in content
    assert "Development Status" in content


def test_scripts_entry_point():
    src_dir = Path(__file__).resolve().parent.parent
    pyproject = src_dir / "pyproject.toml"
    content = pyproject.read_text(encoding="utf-8")
    assert "[project.scripts]" in content
    assert "ctx = " in content


def test_subpackage_imports():
    from ctx_engine import commands
    assert commands is not None

    from ctx_engine import db
    assert db is not None

    from ctx_engine import intelligence
    assert intelligence is not None

    from ctx_engine import languages
    assert languages is not None

    from ctx_engine import daemon
    assert daemon is not None

    from ctx_engine import mcp_server
    assert mcp_server is not None

    from ctx_engine.mcp_server.tools import renderers
    assert renderers is not None
