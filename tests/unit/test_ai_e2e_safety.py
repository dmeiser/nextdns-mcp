"""Unit tests for the E2E harness's secret handling and protection probes.

The key travels as a reference, never a value; the https base is required; the
profile-protection probe fails closed and follows pagination; and an abnormal
exit kills the whole process group. The rest of the harness suite lives in
``test_ai_e2e_harness.py``, ``test_ai_e2e_measure.py``,
``test_ai_e2e_steps.py``, and ``test_ai_e2e_run.py``.

SPDX-License-Identifier: MIT
"""

import asyncio
import json
import os
import tempfile
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO_ROOT / "scripts" / "ai_e2e_harness.py"


@pytest.fixture(autouse=True)
def _stub_probe_httpx(monkeypatch):
    """The profile-protection probe's HTTP answer is faked for the whole module.

    The probe fails closed, so an unstubbed probe would abort every config
    load in tests that set NEXTDNS_API_KEY (and would hit the real network).
    Tests that exercise probe behaviour install their own fake, which
    overrides this one.
    """

    class _Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return {"data": []}

    monkeypatch.setattr("httpx.get", lambda url, **kw: _Resp())


# ===========================================================================
# secret handling: the key travels as a reference, never a value (SEC-4)
# =========================================================================== #


class TestSecretHandling:
    def test_api_base_reads_env_at_call_time(self, h, monkeypatch):
        """Resolved when used, not at import: main() loads dotenv after importing."""
        monkeypatch.setenv("NEXTDNS_API_BASE", "https://api.example.test/base/")
        assert h.api_base() == "https://api.example.test/base"
        monkeypatch.delenv("NEXTDNS_API_BASE")
        assert h.api_base() == "https://api.nextdns.io"

    def test_api_base_raises_the_config_error_on_a_plaintext_base(self, h, monkeypatch):
        """The harness refuses a plaintext base through the server's own getter."""
        from nextdns_mcp.errors import ConfigurationError

        monkeypatch.setenv("NEXTDNS_API_BASE", "http://plain.example.test")
        with pytest.raises(ConfigurationError, match="NEXTDNS_API_BASE must use https://"):
            h.api_base()

    def test_config_server_env_uses_the_api_base_as_loaded(self, h, monkeypatch):
        monkeypatch.setenv("NEXTDNS_API_BASE", "https://late.example")
        cfg = h.HarnessConfig.load(None)
        assert cfg.server["env"]["NEXTDNS_API_BASE"] == "https://late.example"

    def test_redacted_server_env_masks_an_inlined_key(self, h):
        srv = {"env": {"NEXTDNS_API_KEY": "sk-inline-9f3a", "NEXTDNS_TOOL_SEARCH": "off"}}
        out = h._redacted_server_env(srv)
        assert out["NEXTDNS_API_KEY"] == h.ENV_REF_API_KEY
        assert out["NEXTDNS_TOOL_SEARCH"] == "off"
        assert "sk-inline-9f3a" not in json.dumps(out)
        assert h._redacted_server_env({}) == {}
        assert h._redacted_server_env({"env": {}}) == {}

    def test_expand_env_refs_resolves_against_the_process_environment(self, h, monkeypatch):
        monkeypatch.setenv("NEXTDNS_API_KEY", "sk-real-value")
        out = h._expand_env_refs({"NEXTDNS_API_KEY": h.ENV_REF_API_KEY, "NEXTDNS_TOOL_SEARCH": "off"})
        assert out == {"NEXTDNS_API_KEY": "sk-real-value", "NEXTDNS_TOOL_SEARCH": "off"}
        monkeypatch.delenv("NEXTDNS_API_KEY")
        assert h._expand_env_refs({"K": "{env:NEXTDNS_API_KEY}"}) == {"K": ""}

    def test_render_argv_does_not_rescan_substituted_values(self, h):
        """One pass: a placeholder token inside a VALUE must stay literal.

        The prompt is model-facing text; a prompt that spells {server_env}
        would otherwise have the server environment — including the API key —
        expanded into the rendered command line through it.
        """
        argv = h.render_argv(
            ["harness", "{prompt}", "{server_env}"],
            {"{prompt}": "type {server_env} verbatim", "{server_env}": '{"NEXTDNS_API_KEY": "x"}'},
        )
        assert argv == ["harness", "type {server_env} verbatim", '{"NEXTDNS_API_KEY": "x"}']

    def test_run_actor_renders_the_server_env_redacted(self, h, monkeypatch, tmp_path):
        """The {server_env} placeholder that reaches argv never carries the key."""

        async def go():
            cfg = h.HarnessConfig()
            cfg.server["env"] = {"NEXTDNS_API_KEY": "sk-inline-9f3a"}
            captured: dict = {}

            def fake_render(self, values):
                captured.update(values)
                return []  # empty argv -> run_actor stops before spawning anything

            monkeypatch.setattr(h.HarnessConfig, "render_argv", fake_render)
            run = await h.run_actor(cfg, "prompt text", tmp_path)
            assert run.error  # the command-not-found path proves nothing ran
            env_json = captured[h.PLACEHOLDER_SERVER_ENV]
            assert "sk-inline-9f3a" not in env_json
            assert h.ENV_REF_API_KEY in env_json

        import asyncio

        asyncio.run(go())


class TestHttpsBaseRequired:
    """An http base must be refused before the API key is sent anywhere (SEC)."""

    def test_http_base_is_refused_before_any_request(self, h, monkeypatch):
        monkeypatch.setenv("NEXTDNS_API_KEY", "sk-test")
        monkeypatch.setenv("NEXTDNS_API_BASE", "http://plain.example.test")
        calls: list[str] = []
        monkeypatch.setattr("httpx.get", lambda url, **kw: calls.append(url))
        with pytest.raises(ValueError, match="https://"):
            h.HarnessConfig.load(None)
        # The probe never fired: the key never left for a plaintext host.
        assert calls == []

    def test_https_base_proceeds_to_the_probe(self, h, monkeypatch):
        monkeypatch.setenv("NEXTDNS_API_KEY", "sk-test")
        monkeypatch.setenv("NEXTDNS_API_BASE", "https://api.example.test")
        monkeypatch.delenv("NEXTDNS_PROTECTED_PROFILES", raising=False)
        calls: list[str] = []

        class _Resp:
            def raise_for_status(self):
                return None

            def json(self):
                return {"data": [{"id": "pre22"}]}

        def _get(url, **kw):
            calls.append(url)
            return _Resp()

        monkeypatch.setattr("httpx.get", _get)
        cfg = h.HarnessConfig.load(None)
        assert calls == ["https://api.example.test/profiles"]
        assert cfg.server["env"]["NEXTDNS_PROTECTED_PROFILES"] == "pre22"


class TestProfileProtectionProbeFailsClosed:
    """A probe that cannot establish the pre-existing set must abort the run."""

    def test_probe_timeout_aborts_the_config_load(self, h, monkeypatch):
        import httpx

        monkeypatch.setenv("NEXTDNS_API_KEY", "sk-test")
        monkeypatch.delenv("NEXTDNS_PROTECTED_PROFILES", raising=False)

        def _boom(url, **kw):
            raise httpx.ConnectTimeout("timed out reaching the API")

        monkeypatch.setattr("httpx.get", _boom)
        with pytest.raises(h.HarnessAbortError, match="profile-protection probe failed"):
            h.HarnessConfig.load(None)

    def test_probe_http_500_aborts_instead_of_continuing(self, h, monkeypatch):
        import httpx

        monkeypatch.setenv("NEXTDNS_API_KEY", "sk-test")
        monkeypatch.delenv("NEXTDNS_PROTECTED_PROFILES", raising=False)

        class _Resp:
            status_code = 500

            def raise_for_status(self):
                raise httpx.HTTPStatusError("500 Internal Server Error", request=None, response=None)

            def json(self):  # pragma: no cover - raise_for_status fires first
                return {}

        monkeypatch.setattr("httpx.get", lambda url, **kw: _Resp())
        with pytest.raises(h.HarnessAbortError, match="profile-protection probe failed"):
            h.HarnessConfig.load(None)

    def test_probe_unparseable_body_aborts(self, h, monkeypatch):
        import json

        monkeypatch.setenv("NEXTDNS_API_KEY", "sk-test")

        class _Resp:
            def raise_for_status(self):
                return None

            def json(self):
                raise json.JSONDecodeError("bad", "", 0)

        monkeypatch.setattr("httpx.get", lambda url, **kw: _Resp())
        with pytest.raises(h.HarnessAbortError, match="profile-protection probe failed"):
            h.HarnessConfig.load(None)

    def test_probe_without_a_key_is_a_no_op(self, h, monkeypatch):
        monkeypatch.delenv("NEXTDNS_API_KEY", raising=False)
        calls: list[str] = []
        monkeypatch.setattr("httpx.get", lambda url, **kw: calls.append(url))
        h.HarnessConfig.load(None)
        assert calls == []


class TestProtectionProbeFollowsPagination:
    """ProfileList paginates; a one-page probe protects only page one (mcp PR review)."""

    @staticmethod
    def _paging_get(monkeypatch, pages):
        calls: list[dict] = []

        class _Resp:
            def raise_for_status(self):
                return None

            def json(self):
                return pages[len(calls) - 1]

        def _get(url, **kw):
            calls.append(kw.get("params") or {})
            if len(calls) > len(pages):
                raise AssertionError("the probe requested more pages than were served")
            return _Resp()

        monkeypatch.setattr("httpx.get", _get)
        return calls

    def test_probe_protects_every_page_of_the_listing(self, h, monkeypatch):
        monkeypatch.setenv("NEXTDNS_API_KEY", "sk-test")
        monkeypatch.delenv("NEXTDNS_PROTECTED_PROFILES", raising=False)
        calls = self._paging_get(
            monkeypatch,
            [
                {"data": [{"id": "pre1"}], "cursor": "c2"},
                {"data": [{"id": "pre2"}], "meta": {"pagination": {"cursor": None}}},
            ],
        )
        cfg = h.HarnessConfig.load(None)
        assert calls[0].get("cursor") is None
        assert calls[1].get("cursor") == "c2"
        protected = cfg.server["env"]["NEXTDNS_PROTECTED_PROFILES"]
        assert "pre1" in protected and "pre2" in protected

    def test_a_repeated_cursor_aborts_the_run(self, h, monkeypatch):
        """A cursor that never exhausts is a broken API, not an empty list.

        Accepting the repeat as the end of the listing would protect only the
        profiles seen so far and leave every later page's profile writable,
        so the probe fails closed instead of ending the walk as success.
        """
        monkeypatch.setenv("NEXTDNS_API_KEY", "sk-test")
        monkeypatch.delenv("NEXTDNS_PROTECTED_PROFILES", raising=False)
        calls = self._paging_get(
            monkeypatch,
            [
                {"data": [{"id": "pre1"}], "cursor": "c2"},
                {"data": [{"id": "pre2"}], "cursor": "c2"},
            ],
        )
        with pytest.raises(h.HarnessAbortError, match="looped"):
            h.HarnessConfig.load(None)
        assert len(calls) == 2
        # Fail closed: protection is applied only after the walk completes, so
        # the pages seen before the loop protected nothing.

    @pytest.mark.parametrize(
        "page",
        [
            {"data": None},
            {"data": "not-a-list"},
            {"cursor": "c2"},  # data missing entirely
            {"data": [{"id": "pre1"}, "not-a-dict"]},
            {"data": [{"id": "pre1"}, {}]},  # row without an id
            {"data": [{"id": "pre1"}], "cursor": 0},
            {"data": [{"id": "pre1"}], "cursor": ""},
            {"data": [{"id": "pre1"}], "meta": {"pagination": {"cursor": 7}}},
            ["not", "an", "object"],
        ],
        ids=[
            "null-data",
            "non-list-data",
            "missing-data",
            "non-dict-row",
            "idless-row",
            "numeric-cursor",
            "empty-cursor",
            "malformed-nested-cursor",
            "non-object-page",
        ],
    )
    def test_a_malformed_page_aborts_the_run(self, h, monkeypatch, page):
        """Missing/null/non-list data, malformed rows and cursors all fail closed."""
        monkeypatch.setenv("NEXTDNS_API_KEY", "sk-test")
        monkeypatch.delenv("NEXTDNS_PROTECTED_PROFILES", raising=False)
        self._paging_get(monkeypatch, [page])
        with pytest.raises(h.HarnessAbortError):
            h.HarnessConfig.load(None)

    def test_a_neverending_listing_aborts_instead_of_looping(self, h, monkeypatch):
        """Fresh unique cursors forever are a runaway loop, not a huge account."""
        monkeypatch.setenv("NEXTDNS_API_KEY", "sk-test")
        monkeypatch.delenv("NEXTDNS_PROTECTED_PROFILES", raising=False)
        calls: list = []

        class _Resp:
            def raise_for_status(self):
                return None

            def json(self):
                return {"data": [{"id": f"p{len(calls)}"}], "cursor": f"c{len(calls)}"}

        def _get(url, **kw):
            calls.append(kw.get("params") or {})
            return _Resp()

        monkeypatch.setattr("httpx.get", _get)
        with pytest.raises(h.HarnessAbortError, match="exhaust"):
            h.HarnessConfig.load(None)
        assert len(calls) == h._MAX_PROFILE_PAGES

    def test_a_later_page_failure_still_aborts(self, h, monkeypatch):
        import httpx

        monkeypatch.setenv("NEXTDNS_API_KEY", "sk-test")
        monkeypatch.delenv("NEXTDNS_PROTECTED_PROFILES", raising=False)

        class _Resp:
            def __init__(self, payload=None, status=200):
                self._payload = payload
                self._status = status

            def raise_for_status(self):
                if self._status >= 400:
                    raise httpx.HTTPStatusError("500", request=None, response=None)

            def json(self):
                return self._payload

        def _get(url, **kw):
            if kw.get("params", {}).get("cursor"):
                return _Resp(status=500)
            return _Resp({"data": [{"id": "pre1"}], "cursor": "c2"})

        monkeypatch.setattr("httpx.get", _get)
        with pytest.raises(h.HarnessAbortError, match="profile-protection probe failed"):
            h.HarnessConfig.load(None)


class TestAbnormalExitKillsTheProcessGroup:
    """No abnormal path leaves the actor or its MCP server alive."""

    @staticmethod
    async def _spawn():
        return await asyncio.create_subprocess_exec(
            "sleep",
            "30",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )

    @staticmethod
    def _assert_group_reaped(pgid: int) -> None:
        """The group has no live member: signal-0 probe keeps failing.

        A short retry covers the orphaned-grandchild case: an init reaps an
        orphaned zombie on its own schedule, so a single probe could still see
        it milliseconds after the kill.
        """
        import time

        for _ in range(30):
            try:
                os.killpg(pgid, 0)
            except ProcessLookupError:
                return
            time.sleep(0.1)
        pytest.fail(f"process group {pgid} still has a live member")

    def test_timeout_kills_the_group(self, h):
        async def go():
            proc = await self._spawn()
            _out, _err, timed_out = await h._collect_streams(proc, 0.3)
            assert timed_out
            await asyncio.wait_for(proc.wait(), 5)  # reap before probing
            self._assert_group_reaped(proc.pid)

        asyncio.run(go())

    def test_cancellation_kills_the_group(self, h):
        async def go():
            proc = await self._spawn()
            task = asyncio.ensure_future(h._collect_streams(proc, 60))
            await asyncio.sleep(0.3)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            await asyncio.wait_for(proc.wait(), 5)
            self._assert_group_reaped(proc.pid)

        asyncio.run(go())

    def test_keyboard_interrupt_kills_the_group(self, h, monkeypatch):
        import asyncio

        def _interrupt(coro, *args, **kwargs):
            coro.close()  # the harness hands wait_for a fresh proc.wait() coroutine
            raise KeyboardInterrupt

        monkeypatch.setattr(asyncio, "wait_for", _interrupt)

        async def go():
            proc = await self._spawn()
            with pytest.raises(KeyboardInterrupt):
                await h._collect_streams(proc, 60)
            await proc.wait()  # wait_for is patched; the SIGKILL'd child reaps immediately
            await asyncio.sleep(0.1)  # let the drain tasks see their closed pipes
            self._assert_group_reaped(proc.pid)

        asyncio.run(go())

    def test_zero_exit_actor_with_a_live_child_still_loses_its_group(self, h, tmp_path):
        """run_actor tears the group down on EVERY exit shape.

        Reproduced leak: an actor that exits 0 while the server it spawned
        keeps running used to skip the nonzero-exit-only cleanup, leaving the
        whole process group alive. The background sleep detaches from the
        actor's pipes (so the drain ends at the actor's exit, not the 10s
        bound) but stays in the actor's process group.
        """
        marker = tmp_path / "actor-pgid"
        cfg = h.HarnessConfig(command=["sh", "-c", f"sleep 30 >/dev/null 2>&1 & echo $$ > {marker}; exit 0"])
        run = asyncio.run(h.run_actor(cfg, "prompt", tmp_path))
        assert run.ok is True
        self._assert_group_reaped(int(marker.read_text().strip()))

    def test_suppress_swallows_exceptions_but_never_cancellation(self, h):
        """``_suppress`` is a best-effort guard, not a cancellation eater.

        Swallowing ``CancelledError`` (a BaseException since 3.8) inside the
        drain gather used to make a cancelled run keep going with the actor's
        process group still alive. Ordinary cleanup errors stay swallowed;
        cancellation and KeyboardInterrupt propagate.
        """
        with h._suppress():
            raise RuntimeError("ordinary cleanup noise")
        with pytest.raises(asyncio.CancelledError), h._suppress():
            raise asyncio.CancelledError
        with pytest.raises(KeyboardInterrupt), h._suppress():
            raise KeyboardInterrupt

    def test_cancellation_during_the_drain_is_not_swallowed(self, h, monkeypatch):
        """Cancel arriving while the drain gather runs must reap the group AND propagate.

        The drain gather used to run inside ``_suppress``, so a cancellation
        in that window vanished: ``_collect_streams`` returned normally and
        the actor's process group leaked. The same repro at the KeyboardInterrupt
        shape stayed loud; this pins the cancel shape.
        """
        import asyncio

        real_wait_for = asyncio.wait_for
        calls = {"n": 0}

        async def _wait_for(coro, *args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                return await real_wait_for(coro, *args, **kwargs)
            if hasattr(coro, "close"):
                coro.close()  # a coroutine body we never started
            # gather() hands wait_for a Future, which has no close(): abandon it
            raise asyncio.CancelledError

        monkeypatch.setattr(asyncio, "wait_for", _wait_for)

        async def go():
            proc = await self._spawn()
            with pytest.raises(asyncio.CancelledError):
                await h._collect_streams(proc, 60)
            await real_wait_for(proc.wait(), 5)  # the patched wait_for is still in force here
            self._assert_group_reaped(proc.pid)

        asyncio.run(go())

    def test_scratch_cleanup_restores_a_preexisting_explicit_tempdir(self, h, monkeypatch, tmp_path):
        """The scratch restore used to reset ``tempfile.tempdir`` to None.

        A caller that had pinned an explicit tempdir before the run lost the
        pin: every later mktemp in the process re-resolved from TMPDIR. The
        restore must put the pre-run value back. An http API base aborts the
        run at the measuring client's first use - after the scratch exists.
        """
        monkeypatch.setenv("NEXTDNS_API_KEY", "sk-test")
        monkeypatch.setenv("NEXTDNS_API_BASE", "http://plain.example.test")
        sentinel = str(tmp_path / "explicit-tempdir")
        prior = tempfile.tempdir
        had_tmpdir = "TMPDIR" in os.environ
        old_tmpdir = os.environ.get("TMPDIR")
        tempfile.tempdir = sentinel
        try:
            with pytest.raises(ValueError, match="https://"):
                asyncio.run(h.run_harness("sk-test", h.HarnessConfig()))
        finally:
            if had_tmpdir:
                os.environ["TMPDIR"] = old_tmpdir  # type: ignore[index]
            else:
                os.environ.pop("TMPDIR", None)
        assert tempfile.tempdir == sentinel, "the run must not reset an explicit tempdir to None"
        tempfile.tempdir = prior

    def test_concurrent_in_process_runs_serialize_on_the_scratch_globals(self, h, monkeypatch):
        """Two overlapping run_harness calls in one process must not interleave.

        The run mutates process globals (TMPDIR, tempfile.tempdir) for its
        whole lifetime; unserialized, one run's tempfile calls landed in the
        other run's scratch and were deleted with it. The first run parks
        inside its scratch window (the sync wait blocks the run's own event
        loop, so a second run that was NOT serialized could never even enter
        its window before the first exited); the main test thread verifies
        from outside that the second window stays closed, then releases.
        """
        import threading
        import time as _time

        monkeypatch.setenv("NEXTDNS_API_KEY", "sk-test")
        monkeypatch.setenv("NEXTDNS_API_BASE", "http://plain.example.test")
        entered = threading.Event()
        release = threading.Event()
        windows: list[str] = []
        real_prepare = h._prepare_scratch_dir

        def _gated_prepare():
            windows.append("enter")
            entered.set()
            if not release.wait(30):
                pytest.fail("the first run was never released")
            try:
                return real_prepare()
            finally:
                windows.append("exit")

        monkeypatch.setattr(h, "_prepare_scratch_dir", _gated_prepare)
        had_tmpdir = "TMPDIR" in os.environ
        old_tmpdir = os.environ.get("TMPDIR")

        async def run_one():
            with pytest.raises(ValueError, match="https://"):
                await h.run_harness("sk-test", h.HarnessConfig())

        def drive():
            async def go():
                await asyncio.gather(run_one(), run_one())

            asyncio.run(go())

        runner = threading.Thread(target=drive)
        runner.start()
        try:
            assert entered.wait(10)
            _time.sleep(0.5)
            # The first run is parked inside its scratch window: the lock must
            # keep the second run's window closed until the first has exited.
            assert windows == ["enter"], windows
            release.set()
            runner.join(60)
            assert not runner.is_alive(), "the serialized runs did not finish"
            assert windows == ["enter", "exit", "enter", "exit"], windows
        finally:
            release.set()
            runner.join(10)
            if had_tmpdir:
                os.environ["TMPDIR"] = old_tmpdir  # type: ignore[index]
            else:
                os.environ.pop("TMPDIR", None)

    def test_abnormal_actor_exit_reaps_the_spawned_server(self, h, tmp_path):
        """run_actor kills the group when the actor exits non-zero.

        The stand-in actor backgrounds a long sleeper (the MCP server it
        "spawned") and exits 1; the sleeper shares the actor's process group,
        so a live member after run_actor returns is the leak under test.
        """
        marker = tmp_path / "actor-pgid"
        cfg = h.HarnessConfig(command=["sh", "-c", f"sleep 30 & echo $$ > {marker}; exit 1"])
        run = asyncio.run(h.run_actor(cfg, "prompt", tmp_path))
        assert run.error  # the attempt itself failed ...
        assert run.ok is False  # ... and the group was torn down with it
        self._assert_group_reaped(int(marker.read_text().strip()))

    def test_the_scratch_dir_is_reclaimed_when_the_run_aborts(self, h, monkeypatch):
        """Scratch cleanup must not be atexit-only: a run that unwinds mid-way
        (cancellation, Ctrl-C, a mid-run exception) still leaves no 500 MB log
        CSV behind. An http API base aborts the run at the measuring client's
        first use - after the scratch dir exists."""
        monkeypatch.setenv("NEXTDNS_API_KEY", "sk-test")
        monkeypatch.setenv("NEXTDNS_API_BASE", "http://plain.example.test")
        created: dict[str, Path] = {}
        real_prepare = h._prepare_scratch_dir

        def _spying_prepare():
            scratch = real_prepare()
            created["scratch"] = scratch
            return scratch

        monkeypatch.setattr(h, "_prepare_scratch_dir", _spying_prepare)
        # The harness points TMPDIR at the scratch dir process-wide (every
        # per-step MCP server inherits it); put the ambient value back so the
        # leak cannot reach unrelated tests.
        had_tmpdir = "TMPDIR" in os.environ
        old_tmpdir = os.environ.get("TMPDIR")
        try:
            with pytest.raises(ValueError, match="https://"):
                asyncio.run(h.run_harness("sk-test", h.HarnessConfig()))
        finally:
            if had_tmpdir:
                os.environ["TMPDIR"] = old_tmpdir  # type: ignore[index]
            else:
                os.environ.pop("TMPDIR", None)
        assert "scratch" in created, "the run reached the scratch setup"
        assert created["scratch"].exists() is False, "the aborted run left its scratch dir behind"

    def test_a_failing_teardown_does_not_mask_the_in_flight_crash(self, h, monkeypatch, capsys):
        """A raisable teardown site on the crash path is logged, never re-raised.

        A mid-run crash (here a KeyboardInterrupt from a Ctrl-C) is already
        unwinding when the finally runs the teardown. A teardown request that
        itself explodes (the MCP spawn path is the raisable site) must not
        replace the crash: the original exception propagates and the teardown
        failure is recorded, so the profile leak is diagnosed from the report
        instead of a teardown traceback standing in for the crash (mcp PR
        review round 2).
        """
        import asyncio

        config = h.HarnessConfig(None) if None else h.HarnessConfig()
        config.steps = "logs.get"  # a targeted range whose profile is harness-owned

        async def provision(config_, targets):
            return "pid-crash", "provisioned"

        monkeypatch.setattr(h, "_harness_provision_profile", provision)

        async def crash(config_, prompt, workdir, retries=1):
            raise KeyboardInterrupt

        monkeypatch.setattr(h, "run_actor", crash)

        async def wipe(m, pid, targets):
            raise RuntimeError("rest read exploded")

        # The teardown's _profile_gone (REST safety-net read) is a raisable
        # site outside the MCP-delete try/except: a read explosion must be
        # swallowed with the crash recorded, then re-raised as the crash.
        monkeypatch.setattr(h, "_profile_gone", wipe)

        with pytest.raises(KeyboardInterrupt):
            asyncio.run(h.run_harness("sk-test", config))
        out = capsys.readouterr().err
        assert "teardown interrupted while a mid-run crash was unwinding" in out, (
            "the teardown failure was neither surfaced nor suppressed against the crash"
        )
        assert "rest read exploded" in out

    def test_write_mcp_config_redacts_a_key_inlined_by_a_config_file(self, h, tmp_path):
        """--config may inline a key; the generated opencode.json still may not."""
        cfg = h.HarnessConfig()
        cfg.server["env"] = {"NEXTDNS_API_KEY": "sk-inline-9f3a"}
        out = h.write_mcp_config(cfg, tmp_path)
        text = out.read_text(encoding="utf-8")
        assert "sk-inline-9f3a" not in text
        assert "{env:NEXTDNS_API_KEY}" in text
