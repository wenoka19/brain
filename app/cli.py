"""`python -m app.cli doctor|selftest|preview-ax|preview-ui|worker|serve`

`doctor` answers "can this host run brain at all?", `selftest` answers "does the
whole pipeline still work end to end?", and the two previews print or serve
exactly what the founder approved at the two Co-Design Checkpoints.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import sys
import tempfile
import traceback
from pathlib import Path
from typing import Any, Callable

OK = "\033[32m✓\033[0m"
BAD = "\033[31m✗\033[0m"
WARN = "\033[33m!\033[0m"


def _plain() -> bool:
    return not sys.stdout.isatty() or os.environ.get("NO_COLOR") == "1"


def mark(state: str) -> str:
    if _plain():
        return {"ok": "[ ok ]", "bad": "[fail]", "warn": "[warn]"}[state]
    return {"ok": OK, "bad": BAD, "warn": WARN}[state]


def line(state: str, label: str, detail: str = "") -> None:
    print(f"  {mark(state)} {label}" + (f" — {detail}" if detail else ""))


# ===========================================================================
# doctor
# ===========================================================================
def cmd_doctor(args: argparse.Namespace) -> int:
    from app.config import settings

    cfg = settings()
    print(f"\nbrain doctor — env={cfg.env}, data_dir={cfg.data_dir}\n")
    failures = 0
    warnings = 0

    if sys.version_info >= (3, 11):
        line("ok", "python", sys.version.split()[0])
    else:
        line("bad", "python", f"{sys.version.split()[0]} (3.11+ required)")
        failures += 1

    for module, label in (
        ("rapidfuzz", "rapidfuzz (quote grounding)"),
        ("sqlite_vec", "sqlite-vec (semantic lane)"),
        ("jwt", "PyJWT (OAuth tokens)"),
        ("fastapi", "fastapi"),
        ("httpx", "httpx"),
    ):
        try:
            __import__(module)
            line("ok", label)
        except ImportError as exc:
            line("bad", label, str(exc))
            failures += 1

    try:
        probe = sqlite3.connect(":memory:")
        has_fts5 = bool(
            probe.execute(
                "SELECT 1 FROM pragma_compile_options WHERE compile_options LIKE '%FTS5%'"
            ).fetchone()
        )
        line("ok" if has_fts5 else "bad", "SQLite FTS5", sqlite3.sqlite_version)
        failures += 0 if has_fts5 else 1
        import sqlite_vec

        probe.enable_load_extension(True)
        sqlite_vec.load(probe)
        probe.enable_load_extension(False)
        version = probe.execute("SELECT vec_version()").fetchone()[0]
        line("ok", "sqlite-vec extension loads", version)
        probe.close()
    except Exception as exc:
        line("bad", "SQLite capabilities", str(exc))
        failures += 1

    try:
        cfg.ensure_dirs()
        canary = cfg.users_dir / ".doctor"
        canary.write_text("ok")
        canary.unlink()
        line("ok", "data directory writable", str(cfg.data_dir))
    except Exception as exc:
        line("bad", "data directory writable", str(exc))
        failures += 1

    static = Path(__file__).parent / "static"
    missing = [name for name in ("index.html", "app.css", "app.js") if not (static / name).is_file()]
    if missing:
        line("bad", "web assets", f"missing {', '.join(missing)}")
        failures += 1
    else:
        line("ok", "web assets", "index.html, app.css, app.js")

    if cfg.is_test:
        line("ok", "providers", "deterministic (BRAIN_ENV=test)")
    else:
        if os.environ.get("BRAIN_JWT_SECRET"):
            line("ok", "BRAIN_JWT_SECRET set")
        else:
            line("bad", "BRAIN_JWT_SECRET", "required in prod; tokens cannot be signed without it")
            failures += 1
        if os.environ.get("BRAIN_PUBLIC_URL"):
            line("ok", "BRAIN_PUBLIC_URL", os.environ["BRAIN_PUBLIC_URL"])
        else:
            line(
                "warn",
                "BRAIN_PUBLIC_URL",
                "unset — /mcp will answer 421 for any Host it was not told about",
            )
            warnings += 1
        if shutil.which("yt-dlp"):
            line("ok", "yt-dlp on PATH")
        else:
            line("warn", "yt-dlp", "not found — YouTube ingestion will fail")
            warnings += 1

    print(
        f"\n{failures} failure(s), {warnings} warning(s). "
        + ("Ready.\n" if failures == 0 else "Not ready.\n")
    )
    return 1 if failures else 0


# ===========================================================================
# selftest
# ===========================================================================
def cmd_selftest(args: argparse.Namespace) -> int:
    """Exercise the whole product deterministically, asserting as it goes."""
    workdir = Path(tempfile.mkdtemp(prefix="brain-selftest-"))
    os.environ["BRAIN_ENV"] = "test"
    os.environ["BRAIN_DATA_DIR"] = str(workdir)
    os.environ.setdefault("BRAIN_JWT_SECRET", "selftest-secret-key-long-enough-ok")

    # The selftest's own output is the signal; library chatter is noise.
    import logging

    for noisy in ("httpx", "httpcore", "mcp", "mcp.server", "uvicorn"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    from app import config as config_module

    cfg = config_module.reload_settings()
    cfg.ensure_dirs()

    from app import auth, omnibar
    from app.core import engine as E
    from app.core import formatting as F
    from app.db import control as control_db
    from app.db import tenant as tenant_db

    checks: list[tuple[str, Callable[[], Any]]] = []
    failures: list[str] = []

    def check(label: str):
        def register(fn):
            checks.append((label, fn))
            return fn

        return register

    print(f"\nbrain selftest — {workdir}\n")
    control = control_db.control()
    user = control_db.ensure_user(control, "selftest@example.com")
    ctx = E.build_context(user["user_id"], surface="web_ui")

    @check("tenant isolation: a second user sees nothing")
    def _isolation():
        other = control_db.ensure_user(control, "other@example.com")
        assert tenant_db.counts(tenant_db.tenant(other["user_id"]))["claims"] == 0

    @check("ingest: three sources queue instantly")
    def _queue():
        for url in (
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            "https://example.com/report",
            "https://raw.githubusercontent.com/acme/playbooks/main/SKILL.md",
        ):
            result = E.op_ingest_source(ctx, url, user_note="selftest")
            assert result.ok, result.error_message
            assert result.data["status"] == "queued"

    @check("gatekeeper: out-of-scope links are refused")
    def _scope():
        for url, reason in (
            ("https://www.youtube.com/playlist?list=PL1", "playlist_blocked"),
            ("https://example.com/talk.mp4", "raw_media_blocked"),
            ("https://internal.example.com/x", "private_address"),
        ):
            refused = E.op_ingest_source(ctx, url)
            assert not refused.ok and refused.error_reason == reason, url

    @check("pipeline: every job reaches ready")
    def _pipeline():
        processed = E.run_pending_jobs_sync(ctx.user_id, cfg=cfg, limit=10)
        assert len(processed) == 3, processed
        for outcome in processed:
            assert outcome["status"] == "ready", outcome
            assert outcome["claims"] >= 1

    @check("grounding: every stored quote is verbatim in its source")
    def _grounding():
        for row in ctx.tenant.execute(
            "SELECT c.anchor_quote, c.locator_type, sc.full_text "
            "FROM epistemic_claims c JOIN source_content sc ON sc.source_id = c.source_id"
        ).fetchall():
            needle = " ".join(row["anchor_quote"].lower().split())
            haystack = " ".join(row["full_text"].lower().split())
            assert needle in haystack, row["anchor_quote"][:60]
            assert row["locator_type"] in ("timestamp", "page", "section", "none")

    @check("linker: the batching contradiction is found")
    def _tensions():
        assert tenant_db.counts(ctx.tenant)["tensions"] >= 1

    @check("hybrid search: both lanes return and fuse")
    def _search():
        found = E.op_search(ctx, "should I batch my cold emails")
        assert found.ok and found.data["claims"]
        assert any(hit["tensions"] for hit in found.data["claims"])
        assert any(hit["matched_by"] != "semantic" for hit in found.data["claims"])

    @check("search floor: a nonsense query returns nothing")
    def _floor():
        assert E.op_search(ctx, "zzzqqq unrelated nonsense").data["total"] == 0

    @check("Checkpoint A: the search envelope obeys the locked format")
    def _envelope():
        rendered = F.search_envelope(E.op_search(ctx, "batch cold emails"))
        assert "## ⚡ UNRESOLVED DISAGREEMENT — Splits on:" in rendered
        assert "|---" not in rendered
        assert rendered.index(F.HINT) > rendered.index("[UNTRUSTED_LIBRARY_DATA_END]")
        assert F.estimate_tokens(rendered) <= F.TOKEN_BUDGET

    @check("Checkpoint A: the ingestion receipt promises no time")
    def _receipt():
        rendered = F.ingestion_reply(E.op_ingest_source(ctx, "https://example.com/another"))
        assert rendered.startswith("⚡ **Queued**")
        assert "minute" not in rendered.lower()
        assert "only if the user explicitly asks" in rendered

    @check("remember_this: a user axiom can contradict an expert")
    def _axiom():
        result = E.op_remember_this(
            ctx,
            "I never batch cold email; continuous sending always wins for my team",
            "my own outbound sending of cold email",
            domain_tags=["sales"],
        )
        assert result.ok and result.data["new_tensions"]

    @check("skills: synthesise, save, and version")
    def _skills():
        draft = E.op_get_or_synthesize_skill(ctx, "run a cold email outbound sprint")
        assert draft.data["origin"] == "synthesized"
        saved = E.op_save_skill(
            ctx, "cold-email-sprint", "Cold Email Sprint", "d", "outbound push",
            ["Build the list", "Write the sequence"],
        )
        assert saved.data["created"] is True
        again = E.op_save_skill(
            ctx, "cold-email-sprint", "Cold Email Sprint", "d", "outbound push",
            ["Build the list", "Write the sequence", "Send before 10am"],
        )
        assert again.data["skill"]["version"] == 2
        assert E.op_get_or_synthesize_skill(ctx, "run an outbound sprint").data["origin"] == "saved"

    @check("archive: reversible, and it hides the item")
    def _archive():
        claim_id = E.op_search(ctx, "batch cold emails").data["claims"][0]["id"]
        assert E.op_archive_item(ctx, "claim", claim_id).data["status"] == "archived"
        assert claim_id not in [
            hit["id"] for hit in E.op_search(ctx, "batch cold emails").data["claims"]
        ]
        assert E.op_archive_item(ctx, "claim", claim_id, restore=True).data["status"] == "active"

    @check("rescue: a blocked job is recoverable from pasted text")
    def _rescue():
        queued = E.op_ingest_source(ctx, "https://example.com/blocked-source")
        control_db.set_job_status(control, queued.data["job_id"], "blocked_by_platform")
        notes = (
            "We always warm up a brand new sending domain for two full weeks before any real "
            "volume, because the reputation has not been established yet. It breaks down if you "
            "skip the warm up and start at full volume on day one."
        )
        rescued = E.op_rescue_job(ctx, queued.data["job_id"], notes)
        assert rescued.ok and rescued.data["claims"] >= 1

    @check("trash: deletion is gated on it, then irreversible")
    def _trash():
        E.op_seed_demo(ctx)
        refused = E.op_permanent_delete(ctx, E.DEMO_SOURCE_VIDEO)
        assert not refused.ok and refused.error_reason == "not_in_trash"
        E.op_trash_item(ctx, "source", E.DEMO_SOURCE_VIDEO)
        assert E.op_permanent_delete(ctx, E.DEMO_SOURCE_VIDEO).ok
        assert tenant_db.get_source(ctx.tenant, E.DEMO_SOURCE_VIDEO) is None

    @check("omni-bar: a command proposes, then executes, then undoes")
    def _omnibar():
        plan = omnibar.route(ctx, "archive the cold email sprint", submitted=True)
        assert plan.mode == "command" and plan.requires_confirmation
        receipt, _ = omnibar.execute(ctx, plan)
        assert receipt.ok and receipt.undo_action
        assert omnibar.undo(ctx, receipt.undo_action, receipt.undo_params).ok

    @check("omni-bar: a typed delete can only reach Trash")
    def _no_hard_delete():
        plan = omnibar.route(ctx, "delete the Enterprise Outbound Benchmark", submitted=True)
        assert plan.action in ("trash", "archive")
        assert "Nothing is deleted outright" in plan.message

    @check("export: json and markdown both round-trip")
    def _export():
        import json as json_module

        payload = json_module.loads(E.op_export(ctx, "json").data["content"])
        assert payload["claims"] and payload["skills"]
        assert E.op_export(ctx, "markdown").data["content"].startswith("# brain library export")

    @check("oauth: PKCE, audience binding, rotation, revocation")
    def _oauth():
        verifier = "s" * 64
        assert auth.verify_pkce(verifier, auth.pkce_challenge(verifier))
        assert not auth.verify_pkce(verifier, auth.pkce_challenge(verifier), method="plain")
        grant = auth.issue_grant(
            control, user_id=ctx.user_id, client_name="selftest", client_id="c",
            scope=auth.DEFAULT_SCOPE, resource=cfg.audience, cfg=cfg,
        )
        assert auth.verify_access_token(grant["access_token"], cfg=cfg).user_id == ctx.user_id
        rotated = auth.rotate_grant(control, grant["refresh_token"], cfg=cfg)
        assert rotated is not None
        try:
            auth.verify_access_token(grant["access_token"], cfg=cfg)
            raise AssertionError("the pre-rotation token should be dead")
        except auth.TokenRejected:
            pass
        control_db.revoke_session(control, rotated["session_id"])
        try:
            auth.verify_access_token(rotated["access_token"], cfg=cfg)
            raise AssertionError("a revoked token should be dead")
        except auth.TokenRejected:
            pass

    @check("mcp: exactly the 8 tools and 6 namespaced prompts")
    def _mcp():
        import asyncio

        from app import mcp_server

        mcp_server.reset_server()
        server = mcp_server.server(cfg)
        tools = [tool.name for tool in asyncio.run(server.list_tools())]
        assert tools == [
            "search", "fetch", "ingest_source", "get_ingest_status",
            "remember_this", "get_or_synthesize_skill", "save_skill", "archive_item",
        ], tools
        prompts = [prompt.name for prompt in asyncio.run(server.list_prompts())]
        assert prompts and all(name.startswith("brain.") for name in prompts), prompts
        mcp_server.reset_server()

    @check("web: the control plane boots and serves its assets")
    def _web():
        from starlette.testclient import TestClient

        from app.main import create_app

        with TestClient(create_app(cfg)) as client:
            assert client.get("/healthz").json()["status"] == "ok"
            assert client.get("/").status_code == 200
            assert client.get("/preview").status_code == 200
            headers = client.get("/").headers
            assert headers["X-Frame-Options"] == "DENY"
            login = client.post("/api/login", json={"email": "selftest@example.com"})
            token = login.json()["csrf"]
            assert client.post("/api/demo/seed", json={}).status_code == 403  # no CSRF header
            assert client.get("/api/cards").json()["ok"] is True
            assert client.post(
                "/api/demo/seed", json={}, headers={"X-Brain-CSRF": token}
            ).status_code == 200

    @check("audit: every surface wrote a row")
    def _audit():
        surfaces = {
            row["surface"] for row in control_db.recent_audit(control, ctx.user_id, 400)
        }
        assert {"mcp", "web_ui", "web_omnibar", "system"} & surfaces

    for label, fn in checks:
        try:
            fn()
            line("ok", label)
        except Exception as exc:
            failures.append(label)
            line("bad", label, f"{type(exc).__name__}: {exc}")
            if args.verbose:
                traceback.print_exc()

    from app.db import tenant as tenant_module

    tenant_module.close_all()
    control_db.close_all()
    if not args.keep:
        shutil.rmtree(workdir, ignore_errors=True)

    print(f"\n{len(checks) - len(failures)}/{len(checks)} checks passed.")
    if failures:
        print("failed: " + ", ".join(failures) + "\n")
        return 1
    print("selftest green.\n")
    return 0


# ===========================================================================
# preview-ax
# ===========================================================================
def cmd_preview_ax(args: argparse.Namespace) -> int:
    """Print every Checkpoint A envelope against a real seeded library."""
    workdir = Path(tempfile.mkdtemp(prefix="brain-ax-"))
    os.environ["BRAIN_ENV"] = "test"
    os.environ["BRAIN_DATA_DIR"] = str(workdir)

    from app import config as config_module

    cfg = config_module.reload_settings()
    cfg.ensure_dirs()

    from app.core import engine as E
    from app.core import formatting as F
    from app.db import control as control_db

    control = control_db.control()
    user = control_db.ensure_user(control, "preview@example.com")
    ctx = E.build_context(user["user_id"])

    def heading(text: str) -> None:
        print("\n" + "=" * 74)
        print(text)
        print("=" * 74)

    heading("TOUCHPOINT 1 — the mid-chat ingestion receipt")
    first = E.op_ingest_source(ctx, "https://youtu.be/dQw4w9WgXcQ", "for the outbound sprint")
    print(F.ingestion_reply(first))
    print()
    print(F.ingestion_reply(E.op_ingest_source(ctx, "https://example.com/report")))
    print("\n--- refusal ---")
    print(F.ingestion_reply(E.op_ingest_source(ctx, "https://www.youtube.com/playlist?list=PL1")))

    E.run_pending_jobs_sync(ctx.user_id, cfg=cfg)
    print("\n--- duplicate ---")
    print(F.ingestion_reply(E.op_ingest_source(ctx, "https://youtu.be/dQw4w9WgXcQ")))

    heading("TOUCHPOINT 2 — the knowledge & contradiction envelope")
    envelope = F.search_envelope(E.op_search(ctx, "should I batch my cold emails"))
    print(envelope)
    print(f"\n[{F.estimate_tokens(envelope)} tokens of {F.TOKEN_BUDGET} · "
          f"markdown tables: {'|---' in envelope}]")

    heading("TOUCHPOINT 2b — fetch, one claim in full")
    claim_id = E.op_search(ctx, "batch cold emails").data["claims"][0]["id"]
    print(F.fetch_envelope(E.op_fetch(ctx, claim_id)))

    heading("TOUCHPOINT 3a — a synthesized DRAFT")
    print(F.skill_envelope(E.op_get_or_synthesize_skill(ctx, "run a cold email outbound sprint")))

    heading("TOUCHPOINT 3b — a SAVED SKILL")
    backing = [hit["id"] for hit in E.op_search(ctx, "batch cold emails").data["claims"][:3]]
    E.op_save_skill(
        ctx, "cold-email-sprint", "Cold Email Sprint", "Run a one week cold email sprint",
        "Starting an outbound push",
        ["Build the list the night before", "Write the whole sequence in one sitting",
         "Send the batch before 10am"],
        guardrails="Never send on a Friday. Stop batching past 500 sends a day.",
        linked_claim_ids=backing,
    )
    print(F.skill_envelope(E.op_get_or_synthesize_skill(ctx, "run a cold email outbound sprint")))

    heading("OTHER SURFACES")
    print(F.remember_envelope(E.op_remember_this(
        ctx, "I never batch cold email; continuous always wins for my team",
        "my own outbound sending of cold email")))
    print()
    print(F.status_envelope(E.op_get_ingest_status(ctx)))
    print()
    print(F.archive_envelope(E.op_archive_item(ctx, "claim", claim_id)))
    print()
    print(F.refusal(E.op_ingest_source(ctx, "https://internal.example.com/secrets")))

    from app.db import tenant as tenant_module

    tenant_module.close_all()
    control_db.close_all()
    if not args.keep:
        shutil.rmtree(workdir, ignore_errors=True)
    return 0


# ===========================================================================
# preview-ui / serve / worker
# ===========================================================================
def cmd_preview_ui(args: argparse.Namespace) -> int:
    os.environ.setdefault("BRAIN_ENV", "test")
    os.environ["BRAIN_PREVIEW"] = "1"
    os.environ.setdefault("BRAIN_JWT_SECRET", "preview-secret-key-long-enough-ok")
    os.environ.setdefault("BRAIN_ALLOWED_HOSTS", f"{args.host}:{args.port},localhost:{args.port}")

    from app import config as config_module

    cfg = config_module.reload_settings()
    cfg.ensure_dirs()
    if args.seed:
        from app.core import engine as E
        from app.db import control as control_db

        control = control_db.control()
        user = control_db.ensure_user(control, args.seed_email)
        E.op_seed_demo(E.build_context(user["user_id"], surface="web_ui"))
        print(f"seeded the demo library for {args.seed_email}")

    print(f"\n  design harness   http://{args.host}:{args.port}/preview")
    print(f"  the app itself   http://{args.host}:{args.port}/")
    print(f"  data             {cfg.data_dir}\n")
    print("  top bar: layout + edge-state switcher · bottom bar: theme & WCAG tuner\n")

    import uvicorn

    uvicorn.run("app.main:app", host=args.host, port=args.port, log_level=args.log_level)
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    from app.config import settings

    settings().ensure_dirs()
    uvicorn.run("app.main:app", host=args.host, port=args.port, log_level=args.log_level)
    return 0


def cmd_worker(args: argparse.Namespace) -> int:
    from app.worker import main as worker_main

    return worker_main()


# ===========================================================================
# entry point
# ===========================================================================
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app.cli", description="brain CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    doctor = sub.add_parser("doctor", help="check this host can run brain")
    doctor.set_defaults(func=cmd_doctor)

    selftest = sub.add_parser("selftest", help="exercise the whole product deterministically")
    selftest.add_argument("--keep", action="store_true", help="keep the temporary data directory")
    selftest.add_argument("-v", "--verbose", action="store_true", help="print tracebacks")
    selftest.set_defaults(func=cmd_selftest)

    ax = sub.add_parser("preview-ax", help="print the Checkpoint A agent envelopes")
    ax.add_argument("--keep", action="store_true")
    ax.set_defaults(func=cmd_preview_ax)

    ui = sub.add_parser("preview-ui", help="serve the Checkpoint B design harness")
    ui.add_argument("--host", default="127.0.0.1")
    ui.add_argument("--port", type=int, default=8080)
    ui.add_argument("--log-level", default="info")
    ui.add_argument("--no-seed", dest="seed", action="store_false", default=True)
    ui.add_argument("--seed-email", default="founder@example.com")
    ui.set_defaults(func=cmd_preview_ui)

    serve = sub.add_parser("serve", help="run the production server")
    serve.add_argument("--host", default="0.0.0.0")
    serve.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8080)))
    serve.add_argument("--log-level", default="info")
    serve.set_defaults(func=cmd_serve)

    worker = sub.add_parser("worker", help="run the ingestion worker")
    worker.set_defaults(func=cmd_worker)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    sys.exit(main())
