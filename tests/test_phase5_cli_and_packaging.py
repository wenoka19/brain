"""Phase 5 — the CLI, the standalone worker, and the deployment manifests.

`CLAUDE.md` §3 lists four test files; this is a fifth. The packaging is a
deliverable, and a Dockerfile that silently stops copying `app/static` or an
`entrypoint.sh` that stops restoring the replica are exactly the failures that
never show up in a unit test.
"""

from __future__ import annotations

import asyncio
import json
import re
import tomllib
from pathlib import Path

import pytest
import yaml

from app import cli, worker
from app.core import engine as E
from app.db import control as control_db
from app.db import tenant as tenant_db

ROOT = Path(__file__).resolve().parents[1]


# ===========================================================================
# the CLI
# ===========================================================================
class TestCli:
    def test_the_parser_exposes_every_documented_command(self):
        parser = cli.build_parser()
        sub = next(
            action for action in parser._actions if hasattr(action, "choices") and action.choices
        )
        assert {"doctor", "selftest", "preview-ax", "preview-ui"} <= set(sub.choices)
        assert {"serve", "worker", "calibrate"} <= set(sub.choices)

    def test_doctor_passes_on_this_host(self, cfg, capsys):
        assert cli.main(["doctor"]) == 0
        out = capsys.readouterr().out
        assert "0 failure(s)" in out and "Ready." in out
        for probe in ("rapidfuzz", "sqlite-vec extension loads", "SQLite FTS5", "web assets"):
            assert probe in out

    def test_doctor_demands_a_signing_key_in_prod(self, cfg, monkeypatch, capsys):
        monkeypatch.setenv("BRAIN_ENV", "prod")
        monkeypatch.delenv("BRAIN_JWT_SECRET", raising=False)
        monkeypatch.delenv("BRAIN_PUBLIC_URL", raising=False)
        from app import config as config_module

        config_module.reload_settings()
        assert cli.main(["doctor"]) == 1
        out = capsys.readouterr().out
        assert "BRAIN_JWT_SECRET" in out
        assert "Not ready." in out
        # And it warns about the Host allow-list that would 421 every request.
        assert "BRAIN_PUBLIC_URL" in out and "421" in out
        config_module.reload_settings()

    def test_selftest_is_green_and_self_contained(self, cfg, capsys):
        """It must not touch the ambient data directory."""
        before = sorted(p.name for p in cfg.data_dir.glob("*")) if cfg.data_dir.exists() else []
        assert cli.main(["selftest"]) == 0
        out = capsys.readouterr().out
        assert "selftest green." in out
        assert "[fail]" not in out
        passed, total = out.rsplit("\n\n", 2)[-2].split(" ")[0].split("/")
        assert passed == total and int(total) >= 20
        after = sorted(p.name for p in cfg.data_dir.glob("*")) if cfg.data_dir.exists() else []
        assert after == before

    def test_selftest_checks_the_things_that_matter(self, cfg, capsys):
        cli.main(["selftest"])
        out = capsys.readouterr().out
        for claim in (
            "grounding: every stored quote is verbatim",
            "Checkpoint A: the search envelope obeys the locked format",
            "omni-bar: a typed delete can only reach Trash",
            "oauth: PKCE, audience binding, rotation, revocation",
            "trash: deletion is gated on it",
            "tenant isolation",
        ):
            assert claim in out, claim

    def test_preview_ax_prints_all_three_touchpoints(self, cfg, capsys):
        assert cli.main(["preview-ax"]) == 0
        out = capsys.readouterr().out
        assert "TOUCHPOINT 1" in out and "TOUCHPOINT 2" in out and "TOUCHPOINT 3a" in out
        # The locked formats, verbatim.
        assert "⚡ **Queued**" in out
        assert "## ⚡ UNRESOLVED DISAGREEMENT — Splits on:" in out
        assert "✅ **SAVED SKILL" in out and "✏️ **DRAFT — NOT SAVED**" in out
        assert "[UNTRUSTED_LIBRARY_DATA_START]" in out
        assert "markdown tables: False" in out
        assert "minute" not in out.split("TOUCHPOINT 2")[0].lower()

    def test_preview_ui_is_wired_but_not_started_here(self):
        parser = cli.build_parser()
        args = parser.parse_args(["preview-ui"])
        assert args.port == 8080 and args.seed is True
        assert parser.parse_args(["preview-ui", "--no-seed"]).seed is False


# ===========================================================================
# the worker
# ===========================================================================
class TestWorker:
    def test_it_drains_a_queued_job(self, cfg, user):
        ctx = E.build_context(user["user_id"])
        E.op_ingest_source(ctx, "https://www.youtube.com/watch?v=dQw4w9WgXcQ")
        outcome = worker.Worker(cfg=cfg).drain_once()
        assert outcome is not None
        assert outcome["status"] == "ready" and outcome["claims"] >= 1

    def test_it_reports_idle_rather_than_spinning(self, cfg, user):
        assert worker.Worker(cfg=cfg).drain_once() is None

    def test_two_workers_never_take_the_same_job(self, cfg, user):
        ctx = E.build_context(user["user_id"])
        E.op_ingest_source(ctx, "https://www.youtube.com/watch?v=dQw4w9WgXcQ")
        first, second = worker.Worker(cfg=cfg), worker.Worker(cfg=cfg)
        claimed = [first.drain_once(), second.drain_once()]
        assert sum(1 for outcome in claimed if outcome is not None) == 1

    def test_a_crashing_job_fails_cleanly_and_refunds_the_quota(self, cfg, user, monkeypatch):
        ctx = E.build_context(user["user_id"])
        E.op_ingest_source(ctx, "https://www.youtube.com/watch?v=dQw4w9WgXcQ")
        assert control_db.get_user(ctx.control, user["user_id"])["media_minutes_used"] > 0

        def explode(job, *, cfg=None):
            raise RuntimeError("the parser exploded")

        monkeypatch.setattr(worker, "process_job", explode)
        outcome = worker.Worker(cfg=cfg).drain_once()
        assert outcome["status"] == "failed"
        assert "exploded" in outcome["message"]
        assert control_db.get_user(ctx.control, user["user_id"])["media_minutes_used"] == 0

    def test_an_expired_lease_is_recovered(self, cfg, user):
        ctx = E.build_context(user["user_id"])
        E.op_ingest_source(ctx, "https://www.youtube.com/watch?v=dQw4w9WgXcQ")
        leased = control_db.claim_next_job(ctx.control, lease_seconds=worker.LEASE_SECONDS)
        assert worker.Worker(cfg=cfg).drain_once() is None  # still leased
        ctx.control.execute(
            "UPDATE ingestion_jobs SET locked_until = '2000-01-01 00:00:00' WHERE job_id = ?",
            (leased["job_id"],),
        )
        recovered = worker.Worker(cfg=cfg).drain_once()
        assert recovered is not None and recovered["job_id"] == leased["job_id"]

    def test_a_poisonous_job_stops_after_max_attempts(self, cfg, user, monkeypatch):
        ctx = E.build_context(user["user_id"])
        E.op_ingest_source(ctx, "https://www.youtube.com/watch?v=dQw4w9WgXcQ")

        def explode(job, *, cfg=None):
            raise RuntimeError("always fails")

        monkeypatch.setattr(worker, "process_job", explode)
        instance = worker.Worker(cfg=cfg)
        for _ in range(worker.MAX_ATTEMPTS + 2):
            job = control_db.get_job(ctx.control, control_db.list_jobs(ctx.control, ctx.user_id)[0]["job_id"])
            ctx.control.execute("UPDATE ingestion_jobs SET locked_until = NULL, status = 'queued'")
            if instance.drain_once() is None:
                break
        assert len(instance.processed) <= worker.MAX_ATTEMPTS

    def test_the_run_loop_stops_on_request(self, cfg, user):
        ctx = E.build_context(user["user_id"])
        E.op_ingest_source(ctx, "https://www.youtube.com/watch?v=dQw4w9WgXcQ")
        instance = worker.Worker(cfg=cfg, poll_interval=0.01)
        processed = asyncio.run(instance.run(max_jobs=1))
        assert len(processed) == 1
        instance.request_stop()
        assert asyncio.run(instance.run(max_jobs=5)) == processed  # stops immediately


# ===========================================================================
# the deployment manifests
# ===========================================================================
class TestPackaging:
    def test_every_file_the_layout_promises_exists(self):
        for name in (
            "CLAUDE.md", "PROGRESS.md", "DESIGN_DECISIONS.md", "pyproject.toml",
            "Dockerfile", "litestream.yml", "entrypoint.sh", "railway.json", "fly.toml",
            ".claude/setup.sh", ".github/workflows/ci.yml",
        ):
            assert (ROOT / name).is_file(), name
        for module in (
            "config.py", "auth.py", "mcp_server.py", "omnibar.py", "main.py", "cli.py",
            "worker.py", "webapi.py",
            "security/ssrf.py", "security/sanitizer.py", "security/ratelimit.py",
            "db/control.py", "db/tenant.py", "db/search.py",
            "ingestion/gatekeeper.py", "ingestion/providers.py", "ingestion/fetchers.py",
            "ingestion/extractor.py", "ingestion/grounder.py", "ingestion/linker.py",
            "core/engine.py", "core/formatting.py",
            "static/index.html", "static/app.css", "static/app.js",
        ):
            assert (ROOT / "app" / module).is_file(), module

    def test_the_dockerfile_ships_the_web_assets(self):
        text = (ROOT / "Dockerfile").read_text()
        assert "COPY app ./app" in text          # includes app/static
        assert "HEALTHCHECK" in text and "/healthz" in text
        assert "BRAIN_ENV=prod" in text
        assert "litestream" in text
        # Multi-stage: the compiler must not be in the runtime image.
        assert text.count("FROM ") >= 2
        assert "build-essential" in text.split("AS runtime")[0]
        assert "build-essential" not in text.split("AS runtime")[1]

    def test_the_dockerfile_declares_no_volume(self):
        """Railway refuses a build that contains `VOLUME`: there the volume is
        attached to the service and mounted at run time, so a declared
        anonymous volume both conflicts with that and hides the mount. Every
        other target gets the path from its own mount flag.
        """
        lines = [
            line.strip()
            for line in (ROOT / "Dockerfile").read_text().splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        assert not any(line.upper().startswith("VOLUME") for line in lines), lines

    def test_root_is_dropped_by_the_entrypoint_not_by_a_user_instruction(self):
        """A run-time mount shadows the image's `chown`, so the container has to
        start as root to take ownership of it — and then must not stay root,
        because this process parses untrusted PDFs, ZIP archives and HTML.
        """
        dockerfile = (ROOT / "Dockerfile").read_text()
        active = [
            line.strip()
            for line in dockerfile.splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        assert not any(line.startswith("USER ") for line in active), active
        assert "useradd" in dockerfile and "10001" in dockerfile

        entrypoint = (ROOT / "entrypoint.sh").read_text()
        assert 'if [[ "$(id -u)" == "0" ]]' in entrypoint
        assert "chown -R" in entrypoint
        # It must hand over with `exec`, or the dropped process is a child of a
        # root shell and signals stop at the wrong place.
        assert "exec setpriv --reuid" in entrypoint
        assert "exec su " in entrypoint  # fallback when setpriv is absent
        # And the drop has to happen before anything long-running starts.
        assert entrypoint.index("exec setpriv") < entrypoint.index("app.cli serve")

    def test_the_entrypoint_refuses_an_unwritable_data_directory(self):
        """`mkdir -p` on an existing directory succeeds whatever its
        permissions, so a read-only mount would otherwise surface three layers
        deeper as a SQLite error.
        """
        text = (ROOT / "entrypoint.sh").read_text()
        assert '> "${DATA_DIR}/.writable"' in text
        assert "FATAL" in text and "not writable" in text
        assert "exit 1" in text

    def test_the_entrypoint_restores_before_it_serves(self):
        text = (ROOT / "entrypoint.sh").read_text()
        assert "set -euo pipefail" in text
        restore_at = text.index("litestream restore")
        replicate_at = text.index("litestream replicate")
        assert restore_at < replicate_at, "a cold start must restore before writing"
        assert "-if-replica-exists" in text
        assert "BRAIN_ROLE" in text and "worker" in text
        assert text.count("exec ") >= 3  # the server replaces the shell, so signals arrive

    def test_litestream_replicates_control_db_only(self):
        """Tenants are deliberately *not* here.

        A wildcard over `user_*.db` gives Litestream one inotify watcher per
        tenant, which runs out of file descriptors well before 10,000 users.
        Tenant databases are snapshotted by `app/storage/r2_sync.py` instead,
        so this config must stay on `control.db` alone.
        """
        config = yaml.safe_load((ROOT / "litestream.yml").read_text())
        paths = [entry["path"] for entry in config["dbs"]]
        assert any(path.endswith("control.db") for path in paths)
        assert not any("user_" in path for path in paths), (
            "tenant databases must not be watched by Litestream — see r2_sync"
        )
        for entry in config["dbs"]:
            replica = entry["replicas"][0]
            assert replica["sync-interval"] == "30s"
            assert replica["retention"] == "720h"  # 30 days, matching Trash

    def test_the_deploy_manifests_keep_a_single_writer(self):
        railway = json.loads((ROOT / "railway.json").read_text())
        assert railway["deploy"]["numReplicas"] == 1
        assert railway["deploy"]["healthcheckPath"] == "/healthz"
        for secret in ("BRAIN_JWT_SECRET", "BRAIN_PUBLIC_URL"):
            assert secret in railway["_notes"]["required_secrets"]

        fly = tomllib.loads((ROOT / "fly.toml").read_text())
        assert fly["http_service"]["min_machines_running"] == 1
        assert fly["mounts"][0]["destination"] == "/data"
        assert fly["http_service"]["checks"][0]["path"] == "/healthz"
        assert fly["env"]["BRAIN_DATA_DIR"] == "/data"

    def test_ci_runs_the_tests_the_selftest_and_the_image(self):
        workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
        assert set(workflow["jobs"]) >= {"test", "docker"}
        steps = " ".join(
            step.get("run", "") for step in workflow["jobs"]["test"]["steps"]
        )
        assert "pytest" in steps
        assert "app.cli selftest" in steps
        assert "app.cli doctor" in steps
        assert workflow["jobs"]["test"]["env"]["BRAIN_ENV"] == "test"
        assert "3.11" in [str(v) for v in workflow["jobs"]["test"]["strategy"]["matrix"]["python-version"]]
        docker_steps = json.dumps(workflow["jobs"]["docker"]["steps"])
        assert "/healthz" in docker_steps

    def test_setup_script_is_idempotent_and_verifies_the_native_bits(self):
        text = (ROOT / ".claude/setup.sh").read_text()
        assert "set -euo pipefail" in text
        assert "sqlite_vec" in text and "FTS5" in text
        assert "pip install" in text

    def test_every_directly_imported_package_is_declared(self):
        """The drift that shipped: four packages imported and declared nowhere.

        `app/` imported `starlette` and `pydantic` directly while declaring
        neither — both arrived transitively through FastAPI, which works only
        for as long as someone else's dependency keeps supplying them. The test
        suite imported `PyYAML` and `reportlab`, declared in no list at all, so
        it collected only on hosts that happened to have them. And `mcp` and
        `yt-dlp` lived in the `Dockerfile` alone, so `pip install .` produced an
        app that could not import itself.

        This walks the real import graph instead of trusting a list.
        """
        import ast
        import sys
        from importlib.metadata import packages_distributions

        project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]

        def names(specs):
            out = set()
            for spec in specs:
                head = re.split(r"[<>=!~\[;\s]", spec, maxsplit=1)[0]
                out.add(head.lower().replace("_", "-"))
            return out

        runtime = names(project["dependencies"])
        dev = runtime | names(project["optional-dependencies"]["dev"])
        modules = packages_distributions()
        stdlib = set(sys.stdlib_module_names)

        def imported_tops(folder):
            found = set()
            for path in sorted((ROOT / folder).rglob("*.py")):
                if "__pycache__" in path.parts:
                    continue
                for node in ast.walk(ast.parse(path.read_text(), filename=str(path))):
                    if isinstance(node, ast.Import):
                        found.update(a.name.split(".")[0] for a in node.names)
                    elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                        found.add(node.module.split(".")[0])
            return {m for m in found if m not in stdlib and m not in {"app", "tests"}}

        for folder, declared in (("app", runtime), ("tests", dev)):
            for module in sorted(imported_tops(folder)):
                dists = modules.get(module)
                assert dists, (
                    f"{folder}/ imports {module!r}, which maps to no installed "
                    f"distribution — cannot verify it is declared"
                )
                canonical = {d.lower().replace("_", "-") for d in dists}
                assert canonical & declared, (
                    f"{folder}/ imports {module!r} (from {sorted(canonical)}) but "
                    f"pyproject.toml does not declare it"
                )

    def test_the_mcp_floor_matches_the_module_paths_the_code_imports(self):
        """`mcp>=1.2` was a green build that failed at import.

        `app/mcp_server.py` imports `mcp.server.mcpserver.MCPServer` and
        `mcp.server.transport_security`, which exist only in 2.x.
        """
        project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
        spec = next(s for s in project["dependencies"] if s.startswith("mcp"))
        assert ">=2" in spec, spec

        source = (ROOT / "app/mcp_server.py").read_text()
        assert "from mcp.server.mcpserver import MCPServer" in source
        assert "mcp.server.transport_security" in (ROOT / "app/main.py").read_text()

    def test_no_second_dependency_list_exists(self):
        """Three lists existed and two had drifted. Now there is one, and the
        other two read it. A restated list is the bug, so assert its absence."""
        pins = re.compile(r"[\"'][A-Za-z0-9_.-]+(\[[a-z,]+\])?>=[0-9]")
        for name in ("Dockerfile", ".claude/setup.sh"):
            text = (ROOT / name).read_text()
            assert not pins.search(text), (
                f"{name} restates a pinned dependency; it must read pyproject.toml instead"
            )
            assert "pyproject.toml" in text, f"{name} never reads pyproject.toml"
            assert "tomllib" in text, f"{name} does not parse pyproject.toml"

    def test_nothing_committed_carries_a_real_secret(self):
        """A placeholder is fine; a key is not."""
        suspicious = re.compile(
            r"(sk-[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16}|ghp_[A-Za-z0-9]{36}|-----BEGIN [A-Z ]*PRIVATE KEY)"
        )
        for path in ROOT.rglob("*"):
            if not path.is_file() or ".git/" in str(path) or ".brain_data" in str(path):
                continue
            if path.suffix in (".py", ".sh", ".yml", ".yaml", ".toml", ".json", ".md", ".html", ".css", ".js"):
                assert not suspicious.search(path.read_text(errors="ignore")), path


class TestCalibrate:
    """The floor-calibration harness. Its job is to measure, and to refuse to
    invent a number it cannot derive."""

    def test_it_measures_both_distributions_and_confirms_the_search_floor(self, cfg, capsys):
        assert cli.main(["calibrate"]) == 0
        out = capsys.readouterr().out
        assert "query -> claim" in out
        assert "on-topic" in out and "off-topic" in out
        assert "same topic" in out and "cross topic" in out
        assert "search_similarity_floor" in out
        # The shipped 0.18 is confirmed by measurement, not asserted.
        assert "shipped search floor" in out
        assert "confirmed" in out

    def test_it_reports_every_proposed_link_for_judgement(self, cfg, capsys):
        cli.main(["calibrate"])
        out = capsys.readouterr().out
        assert "every link the classifier proposed" in out
        assert "judgement call about your own sources, not a statistic" in out
        # The measured false positive is visibly excluded by the shipped floor.
        assert "✂" in out
        assert "warehouse" in out and "renewal" in out

    def test_it_names_the_active_embedder_and_extractor(self, cfg, capsys):
        cli.main(["calibrate"])
        out = capsys.readouterr().out
        assert "deterministic_v1" in out
        assert "heuristic-v1" in out
        assert "@ 256d" in out

    def test_it_measures_against_a_bare_scale(self, cfg, capsys):
        """The floors being calibrated must not bound their own measurement."""
        import inspect

        source = inspect.getsource(cli.cmd_calibrate)
        assert 'BRAIN_SEARCH_FLOOR="0"' in source
        assert 'BRAIN_LINK_FLOOR="0"' in source

    def test_no_command_leaks_its_scratch_environment(self, cfg, capsys):
        """A scratch data dir or a pinned floor must not outlive the command."""
        import os

        from app import config as config_module

        config_module.reload_settings()
        before = (
            os.environ.get("BRAIN_SEARCH_FLOOR"),
            os.environ.get("BRAIN_LINK_FLOOR"),
            str(config_module.settings().data_dir),
            config_module.settings().search_similarity_floor,
        )
        for command in (["calibrate"], ["preview-ax"], ["selftest"]):
            cli.main(command)
            capsys.readouterr()
            assert (
                os.environ.get("BRAIN_SEARCH_FLOOR"),
                os.environ.get("BRAIN_LINK_FLOOR"),
                str(config_module.settings().data_dir),
                config_module.settings().search_similarity_floor,
            ) == before, command

    def test_it_accepts_a_custom_corpus(self, cfg):
        parser = cli.build_parser()
        args = parser.parse_args(
            ["calibrate", "--source", "a=https://x/1", "--source", "b=https://y/2",
             "--query", "a=something"]
        )
        assert args.source == ["a=https://x/1", "b=https://y/2"]
        assert args.query == ["a=something"]

    def test_it_leaves_no_data_behind(self, cfg, capsys):
        before = sorted(p.name for p in cfg.data_dir.glob("*")) if cfg.data_dir.exists() else []
        cli.main(["calibrate"])
        capsys.readouterr()
        after = sorted(p.name for p in cfg.data_dir.glob("*")) if cfg.data_dir.exists() else []
        assert after == before
