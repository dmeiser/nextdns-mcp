"""Regression tests (issue #177): importing the server must not load a .env file.

Previously ``load_dotenv()`` ran at module import time, so importing
``nextdns_mcp.server`` from any working directory walked upward and loaded
whatever ``.env`` file it found (e.g. a ``NEXTDNS_API_KEY``) into the importing
process. ``load_dotenv()`` now only runs in the ``__main__`` entrypoint.
"""

import subprocess
import sys


class TestImportDoesNotLoadDotenv:
    """Importing nextdns_mcp.server must have no dotenv side effects."""

    def test_importing_server_does_not_mutate_environ_from_stray_env_file(self, tmp_path):
        # A stray .env in the working directory (and one further up the tree,
        # which is what the old import-time load_dotenv() walk would find) must
        # never leak into the environment of a process that only imports the
        # server module.
        stray_dir = tmp_path / "cwd"
        stray_dir.mkdir()
        (tmp_path / ".env").write_text("NEXTDNS_API_KEY=leaked_from_stray_env\n")
        (stray_dir / ".env").write_text("NEXTDNS_API_KEY=leaked_from_cwd_env\n")

        code = (
            "import os, sys\n"
            "assert 'NEXTDNS_API_KEY' not in os.environ\n"
            "import nextdns_mcp.server\n"
            "assert 'NEXTDNS_API_KEY' not in os.environ\n"
            "print('ok')\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            check=False,
            cwd=stray_dir,
            env={"PYTHONPATH": "", "PATH": "/usr/bin:/bin"},
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "ok"

    def test_entrypoint_still_loads_dotenv(self, tmp_path, monkeypatch):
        # The real entrypoint must still load .env: ``python -m nextdns_mcp.server``
        # reads variables from a .env in the working directory before config is
        # first read, so existing launch instructions keep working.
        env_file = tmp_path / ".env"
        env_file.write_text("NEXTDNS_API_KEY=from_env_file\nNEXTDNS_HTTP_TIMEOUT=12\n")

        code = (
            "import os, runpy\n"
            "try:\n"
            "    runpy.run_module('nextdns_mcp.server', run_name='__main__')\n"
            "except SystemExit:\n"
            "    pass\n"
            "assert os.environ.get('NEXTDNS_API_KEY') == 'from_env_file'\n"
            "assert os.environ.get('NEXTDNS_HTTP_TIMEOUT') == '12'\n"
            "print('ok')\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            check=False,
            cwd=tmp_path,
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "ok"
