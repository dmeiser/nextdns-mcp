"""Regression tests: importing the server must have no side effects.

Issue #177: ``load_dotenv()`` ran at module import time, so importing
``nextdns_mcp.server`` from any working directory walked upward and loaded
whatever ``.env`` file it found (e.g. a ``NEXTDNS_API_KEY``) into the importing
process. ``load_dotenv()`` now only runs in the ``__main__`` entrypoint.

Issue #190: the same module also set ``FASTMCP_CHECK_FOR_UPDATES`` in the
environment and built the server singleton at import time, so merely importing
the module changed the importing process. Both now live in the explicit
``configure()`` factory that the entrypoint and the tests call deliberately.

Issue #291: ``_sync_fastmcp_settings()`` read the variable with a bare
``os.environ[...]`` subscript, so calling it without ``configure()`` having set
it first raised ``KeyError``. It now falls back to the same ``"off"`` default.
"""

import os
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
            stdin=subprocess.DEVNULL,
            timeout=30,
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "ok"


class TestImportHasNoServerSideEffects:
    """Importing nextdns_mcp.server must build nothing and mutate nothing."""

    def test_importing_server_creates_no_instance_and_mutates_no_environ(self, tmp_path):
        # Importing the server module must not construct a FastMCP instance and
        # must not change the environment of the importing process. FASTMCP is
        # imported first so that its own (import-time) work is excluded from the
        # comparison and only nextdns_mcp.server's effects are measured.
        code = (
            "import os\n"
            "import fastmcp\n"
            "created = []\n"
            "original_init = fastmcp.FastMCP.__init__\n"
            "def spy(self, *args, **kwargs):\n"
            "    created.append('FastMCP')\n"
            "    return original_init(self, *args, **kwargs)\n"
            "fastmcp.FastMCP.__init__ = spy\n"
            "before = dict(os.environ)\n"
            "import nextdns_mcp.server as server\n"
            "after = dict(os.environ)\n"
            "assert created == [], created\n"
            "assert server._mcp_server is None\n"
            "assert after == before, sorted(set(after) ^ set(before))\n"
            "assert 'FASTMCP_CHECK_FOR_UPDATES' not in after\n"
            "print('ok')\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            check=False,
            cwd=str(tmp_path),
            env={"PYTHONPATH": "", "PATH": "/usr/bin:/bin"},
            stdin=subprocess.DEVNULL,
            timeout=60,
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "ok"

    def test_configure_applies_the_side_effects_on_demand(self, monkeypatch):
        """configure() is what disables the update check and builds the server."""
        import fastmcp

        from nextdns_mcp import server

        monkeypatch.delenv("FASTMCP_CHECK_FOR_UPDATES", raising=False)
        monkeypatch.setattr(fastmcp.settings, "check_for_updates", "stable")
        monkeypatch.setattr(server, "_mcp_server", None)

        built = server.configure()

        assert os.environ["FASTMCP_CHECK_FOR_UPDATES"] == "off"
        assert fastmcp.settings.check_for_updates == "off"
        assert built is server.get_mcp_server()

    def test_sync_does_not_require_the_env_vars_to_be_set(self, monkeypatch):
        """The helper must not crash when it is called on its own (issue #291)."""
        import fastmcp

        from nextdns_mcp import server

        for env_var, _attribute, _default in server._FASTMCP_RUNTIME_DEFAULTS:
            monkeypatch.delenv(env_var, raising=False)
        monkeypatch.setattr(fastmcp.settings, "check_for_updates", "stable")

        server._sync_fastmcp_settings()

        assert fastmcp.settings.check_for_updates == "off"

    def test_configure_respects_an_explicit_update_check_opt_in(self, monkeypatch):
        """An operator who set FASTMCP_CHECK_FOR_UPDATES keeps their choice."""
        import fastmcp

        from nextdns_mcp import server

        monkeypatch.setenv("FASTMCP_CHECK_FOR_UPDATES", "stable")
        monkeypatch.setattr(server, "_mcp_server", None)

        server.configure()

        assert fastmcp.settings.check_for_updates == "stable"

    def test_entrypoint_applies_the_update_check_disable(self, monkeypatch):
        """The shipped `python -m nextdns_mcp.server` path still disables update checks.

        Import is now inert, so the entrypoint's own start path is what must
        perform the disable; otherwise the server would start doing network
        update checks in offline/CI environments again.
        """
        import fastmcp

        from nextdns_mcp import server

        ran: list = []
        monkeypatch.setenv("NEXTDNS_API_KEY", "test_api_key_12345")
        monkeypatch.delenv("FASTMCP_CHECK_FOR_UPDATES", raising=False)
        monkeypatch.setattr(fastmcp.settings, "check_for_updates", "stable")
        monkeypatch.setattr(server, "_mcp_server", None)
        monkeypatch.setattr(server, "configure_logging", lambda: None)
        monkeypatch.setattr(fastmcp.FastMCP, "run", lambda self, **kwargs: ran.append(kwargs))

        server._run_server()

        assert ran == [{}]  # stdio transport, no extra options
        assert os.environ["FASTMCP_CHECK_FOR_UPDATES"] == "off"
        assert fastmcp.settings.check_for_updates == "off"
