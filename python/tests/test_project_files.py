# ABOUTME: Verifies the project includes the operational files needed to run the terminal service.
# ABOUTME: Checks the launch script exists, parses cleanly, and the README documents the main commands.

from pathlib import Path
import subprocess


ROOT_DIR = Path(__file__).resolve().parents[2]
README_PATH = ROOT_DIR / "README.md"
START_SCRIPT_PATH = ROOT_DIR / "scripts" / "start_termweb.sh"


def test_readme_documents_start_command():
    content = README_PATH.read_text(encoding="utf-8")

    assert "python3 -m pytest -q python/tests/test_web_terminal_server.py" in content
    assert "scripts/start_termweb.sh" in content
    assert "com.termweb.web-terminal" in content


def test_start_script_exists_and_parses():
    assert START_SCRIPT_PATH.exists()
    assert START_SCRIPT_PATH.is_file()

    subprocess.run(
        ["zsh", "-n", str(START_SCRIPT_PATH)],
        check=True,
        cwd=ROOT_DIR,
    )
