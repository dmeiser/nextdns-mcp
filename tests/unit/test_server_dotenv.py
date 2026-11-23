"""Regression tests: importing the server must have no side effects.

Issue #177: ``load_dotenv()`` ran at module import time, so importing
``nextdns_mcp.server`` from any working directory walked upward and loaded
whatever ``.env`` file it found (e.g. a ``NEXTDNS_API_KEY``) into the importing
process. ``load_dotenv()`` now only runs in the entrypoints.

Issue #190: the same module also set ``FASTMCP_CHECK_FOR_UPDATES`` in the
environment and built the server singleton at import time, so merely importing
the module changed the importing process. Both now live in the explicit
``configure()`` factory that the entrypoint and the tests call deliberately.

Issue #291: the update-check helper that used to mirror the variable into
FastMCP's settings read it with a bare ``os.environ[...]`` subscript and
raised ``KeyError`` when called on its own. That helper is gone (issue #643):
``configure()`` no longer writes to FastMCP's live settings object at all.

Issue #643: ``configure()`` used to mutate ``sys.modules["fastmcp"].settings`` -
a hidden global write to a third-party settings object. The only channel now is
the environment variable, which FastMCP reads when it builds its settings
object at import time. Because ``server.py`` imports fastmcp before its own
``__main__`` block runs, the ``python -m nextdns_mcp.server`` entrypoint
re-executes through the package entrypoint (``python -m nextdns_mcp``) whenever
the variable was not exported by the parent shell, so the ``"off"`` default and
any ``.env`` value reach FastMCP's settings before it loads; a shell-exported
value already took effect and needs no re-exec.
"""

import importlib
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
            cwd=str(stray_dir),
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
            "assert server.mcp_server_owner.get() is None\n"
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


class TestConfigureUpdateCheckContract:
    """configure() defaults the env variable but never writes to FastMCP's settings."""

    def test_configure_applies_the_side_effects_on_demand(self, monkeypatch):
        """configure() is what applies the env default and builds the server."""
        from nextdns_mcp import server

        monkeypatch.delenv("FASTMCP_CHECK_FOR_UPDATES", raising=False)

        token = server.mcp_server_owner.set(None)
        try:
            built = server.configure()
            assert built is server.get_mcp_server()
        finally:
            server.mcp_server_owner.reset(token)

        assert os.environ["FASTMCP_CHECK_FOR_UPDATES"] == "off"

    def test_configure_writes_nothing_to_fastmcp_settings(self, monkeypatch):
        """issue #643: a preloaded FastMCP settings object must come out untouched.

        This is the regression oracle: configure() used to overwrite
        ``fastmcp.settings.check_for_updates`` through ``sys.modules``, a
        hidden global mutation of a third-party settings object.
        """
        import fastmcp

        from nextdns_mcp import server

        monkeypatch.delenv("FASTMCP_CHECK_FOR_UPDATES", raising=False)
        monkeypatch.setattr(fastmcp.settings, "check_for_updates", "stable")

        token = server.mcp_server_owner.set(None)
        try:
            server.configure()
        finally:
            server.mcp_server_owner.reset(token)

        assert fastmcp.settings.check_for_updates == "stable"

    def test_configure_respects_an_explicit_update_check_opt_in(self, monkeypatch):
        """An operator who set FASTMCP_CHECK_FOR_UPDATES keeps their choice."""
        import fastmcp

        from nextdns_mcp import server

        monkeypatch.setenv("FASTMCP_CHECK_FOR_UPDATES", "stable")
        monkeypatch.setattr(fastmcp.settings, "check_for_updates", "stable")

        token = server.mcp_server_owner.set(None)
        try:
            server.configure()
        finally:
            server.mcp_server_owner.reset(token)

        assert os.environ["FASTMCP_CHECK_FOR_UPDATES"] == "stable"
        assert fastmcp.settings.check_for_updates == "stable"


class TestPackageEntrypointDefaultsUpdateCheck:
    """`python -m nextdns_mcp` (the module entrypoint's re-exec target) defaults the check before FastMCP loads."""

    def _run_entry(self, tmp_path, extra_env):
        """Run the package entrypoint far enough to observe FastMCP's import-time settings.

        ``main()`` raises ConfigurationError from the bogus MCP_TRANSPORT after
        ``configure()`` has built the server, so the process can inspect
        ``fastmcp.settings`` and exit without ever serving traffic.
        """
        env = {
            "PYTHONPATH": "",
            "PATH": "/usr/bin:/bin",
            "NEXTDNS_API_KEY": "test_api_key_12345",
            "MCP_TRANSPORT": "bogus",
        }
        env.update(extra_env)
        code = (
            "import sys\n"
            "from nextdns_mcp.config import ConfigurationError\n"
            "import nextdns_mcp.__main__ as entry\n"
            "assert 'fastmcp' not in sys.modules, 'entry module pulled fastmcp in at import time'\n"
            "try:\n"
            "    entry.main()\n"
            "except ConfigurationError:\n"
            "    pass\n"
            "else:\n"
            "    raise SystemExit('main() should have raised for the bogus MCP_TRANSPORT')\n"
            "import fastmcp\n"
            "print(fastmcp.settings.check_for_updates)\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            check=False,
            cwd=str(tmp_path),
            env=env,
            stdin=subprocess.DEVNULL,
            timeout=120,
        )
        assert result.returncode == 0, result.stderr
        return result.stdout.strip()

    def test_entrypoint_defaults_update_check_to_off(self, tmp_path):
        assert self._run_entry(tmp_path, {}) == "off"

    def test_entrypoint_preserves_an_explicit_operator_opt_in(self, tmp_path):
        assert self._run_entry(tmp_path, {"FASTMCP_CHECK_FOR_UPDATES": "stable"}) == "stable"

    def test_main_defaults_the_env_and_runs_the_server(self, monkeypatch):
        """main() itself: the env default is applied before the server runs.

        The subprocess tests above prove the ordering against a fresh
        interpreter; this one covers main()'s body in-process, with the .env
        load and the server runner stubbed so nothing blocks. The server
        module is resolved through ``importlib.import_module`` (not
        ``from nextdns_mcp import server``): the autouse
        ``restore_nextdns_modules`` fixture can leave a stale
        ``nextdns_mcp.server`` package attribute behind, and ``main()`` binds
        ``_run_server`` from a fresh ``sys.modules`` import - the stale object
        is not the one main() will call.
        """
        import nextdns_mcp.__main__ as entry

        server = importlib.import_module("nextdns_mcp.server")

        monkeypatch.delenv("FASTMCP_CHECK_FOR_UPDATES", raising=False)
        ran: list = []
        monkeypatch.setattr(entry, "load_dotenv", lambda *args, **kwargs: None)
        monkeypatch.setattr(server, "_run_server", lambda: ran.append(True))

        entry.main()

        assert os.environ["FASTMCP_CHECK_FOR_UPDATES"] == "off"
        assert ran == [True]


class TestModuleEntrypointDefaultsUpdateCheck:
    """`python -m nextdns_mcp.server` keeps its off-by-default by re-execing through the package entrypoint."""

    def _stub_entrypoint_parts(self, monkeypatch):
        """Stub the .env load and the server runner; return their call logs.

        The re-exec stub also records, then raises SystemExit to model
        os.execvpe, which replaces the process image and never returns.
        """
        import dotenv

        from nextdns_mcp import server

        calls: dict = {"load_dotenv": [], "reexec": [], "run_server": []}

        def fake_reexec():
            calls["reexec"].append(True)
            raise SystemExit(0)

        monkeypatch.setattr(dotenv, "load_dotenv", lambda *args, **kwargs: calls["load_dotenv"].append(True))
        monkeypatch.setattr(server, "_reexec_through_package_entrypoint", fake_reexec)
        monkeypatch.setattr(server, "_run_server", lambda: calls["run_server"].append(True))
        return calls

    def test_reexecs_when_the_variable_was_not_shell_exported(self, monkeypatch):
        """The first process imported fastmcp without the default, so it must start over."""
        import pytest

        from nextdns_mcp import server

        monkeypatch.delenv("FASTMCP_CHECK_FOR_UPDATES", raising=False)
        calls = self._stub_entrypoint_parts(monkeypatch)

        with pytest.raises(SystemExit):
            server._run_module_entrypoint()

        assert calls["reexec"] == [True]
        assert calls["run_server"] == []
        assert os.environ["FASTMCP_CHECK_FOR_UPDATES"] == "off"

    def test_reexecs_for_a_value_that_only_the_env_file_provides(self, monkeypatch):
        """A .env value also needs the re-exec: fastmcp was imported before load_dotenv ran."""
        import dotenv
        import pytest

        from nextdns_mcp import server

        monkeypatch.delenv("FASTMCP_CHECK_FOR_UPDATES", raising=False)
        calls = self._stub_entrypoint_parts(monkeypatch)

        def fake_load_dotenv(*args, **kwargs):
            calls["load_dotenv"].append(True)
            monkeypatch.setenv("FASTMCP_CHECK_FOR_UPDATES", "stable")

        monkeypatch.setattr(dotenv, "load_dotenv", fake_load_dotenv)

        with pytest.raises(SystemExit):
            server._run_module_entrypoint()

        assert calls["reexec"] == [True]
        assert calls["run_server"] == []
        assert os.environ["FASTMCP_CHECK_FOR_UPDATES"] == "stable"

    def test_runs_in_process_when_the_variable_is_shell_exported(self, monkeypatch):
        """A shell-exported value already took effect at import time: no re-exec needed."""
        from nextdns_mcp import server

        monkeypatch.setenv("FASTMCP_CHECK_FOR_UPDATES", "stable")
        calls = self._stub_entrypoint_parts(monkeypatch)

        server._run_module_entrypoint()

        assert calls["reexec"] == []
        assert calls["run_server"] == [True]
        assert os.environ["FASTMCP_CHECK_FOR_UPDATES"] == "stable"

    def test_real_command_starts_through_the_package_entrypoint(self, tmp_path):
        """End to end: the real `python -m nextdns_mcp.server` with the variable unset.

        The re-exec is real here (os.execvpe replaces the process image), so
        the run options failure comes from the package entrypoint's start-up -
        proving the shipped command still reaches FastMCP with the default
        applied and does not die in the hand-off.
        """
        result = subprocess.run(
            [sys.executable, "-m", "nextdns_mcp.server"],
            capture_output=True,
            text=True,
            check=False,
            cwd=str(tmp_path),
            env={
                "PYTHONPATH": "",
                "PATH": "/usr/bin:/bin",
                "NEXTDNS_API_KEY": "test_api_key_12345",
                "MCP_TRANSPORT": "bogus",
            },
            stdin=subprocess.DEVNULL,
            timeout=120,
        )
        assert result.returncode != 0, result.stdout
        assert "Invalid MCP_TRANSPORT" in result.stderr, result.stderr
