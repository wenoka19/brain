"""`R-17` — storage is not a lever, and a received file is not an asset.

The founder's two rulings, together:

  *"In the usage screen we shouldn't show the storage size. We shouldn't show
  storage."*

  *"What we receive is a link, a photo, and a document. After processing them
  there is no problem with the link, the image, or the document. I think we
  should keep them for 30 days and when those 30 days pass they should be
  deleted."*

The second is what makes the first honest. Striking the storage meter while
uploads accumulate forever would only hide a number that was growing; the
sweep is what bounds it. These tests hold both halves: the sweep deletes what
it should, keeps what it should, and survives the things housekeeping runs
into — and the leak it closes stays closed, because the test that proves a
successful upload used to be kept forever is the first one below.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from app.core import engine as E
from app.storage import retention


DAY = 86400.0


def _aged(path: Path, days: float, *, body: bytes = b"x" * 64) -> Path:
    """Write a file and backdate it. The alternative is a test that waits."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    moment = time.time() - days * DAY
    import os

    os.utime(path, (moment, moment))
    return path


class TestTheUploadIsNotKeptForever:
    """The leak this closes. `api_upload` writes the bytes and `op_ingest_file`
    queues a job against that path; nothing ever deleted them on the success
    path — the one `unlink` in the route is the over-size branch."""

    def test_the_route_only_unlinks_on_the_failure_path(self):
        """Not a style point: this is the shape of the bug. If a future edit
        deletes the file on success instead, the 30-day window the founder
        asked for silently becomes zero."""
        source = (Path(__file__).resolve().parents[1] / "app/webapi.py").read_text()
        route = source[source.index("async def api_upload") : source.index("async def api_jobs")]
        assert route.count("unlink") == 1
        over_size = route[route.index("if written >") :]
        assert "unlink" in over_size, "the only unlink must be the over-size branch"

    def test_a_processed_upload_still_exists_the_day_after(self, cfg, user):
        """Thirty days, not zero. A failed extraction is reproducible only
        while the bytes are there, which is the whole reason for the window."""
        upload = _aged(cfg.uploads_dir_for(user["user_id"]) / "up_1_notes.txt", days=1)
        retention.sweep_uploads(cfg=cfg)
        assert upload.exists()

    def test_and_is_gone_once_the_window_passes(self, cfg, user):
        upload = _aged(cfg.uploads_dir_for(user["user_id"]) / "up_1_notes.txt", days=31)
        swept = retention.sweep_uploads(cfg=cfg)
        assert not upload.exists()
        assert swept.files == 1
        assert swept.bytes == 64


class TestTheSweepItself:
    def test_the_boundary_is_the_configured_window(self, cfg, user):
        """`upload_retention_days`, not a literal 30 anywhere in the sweep."""
        folder = cfg.uploads_dir_for(user["user_id"])
        young = _aged(folder / "up_young.txt", days=cfg.upload_retention_days - 1)
        old = _aged(folder / "up_old.txt", days=cfg.upload_retention_days + 1)
        retention.sweep_uploads(cfg=cfg)
        assert young.exists() and not old.exists()

    def test_a_shorter_window_sweeps_more(self, cfg, user):
        """The window is a setting, so a deployment can shorten it without a
        code change — and the test proves the setting is what is read."""
        from dataclasses import replace

        folder = cfg.uploads_dir_for(user["user_id"])
        file = _aged(folder / "up_1.txt", days=3)
        retention.sweep_uploads(cfg=cfg)
        assert file.exists()
        retention.sweep_uploads(cfg=replace(cfg, upload_retention_days=2))
        assert not file.exists()

    def test_one_tenant_is_never_swept_by_another_s_window(self, cfg, control):
        """Per-user folders, and the sweep walks them all — but a file is
        judged on its own age, never on a sibling's."""
        from app.db import control as control_db

        a = control_db.ensure_user(control, "a@example.com")["user_id"]
        b = control_db.ensure_user(control, "b@example.com")["user_id"]
        keep = _aged(cfg.uploads_dir_for(a) / "up_new.txt", days=2)
        drop = _aged(cfg.uploads_dir_for(b) / "up_old.txt", days=40)
        retention.sweep_uploads(cfg=cfg)
        assert keep.exists() and not drop.exists()

    def test_an_emptied_folder_is_removed(self, cfg, user):
        """An account that uploaded once two years ago should not leave a
        folder behind forever."""
        folder = cfg.uploads_dir_for(user["user_id"])
        _aged(folder / "up_old.txt", days=40)
        swept = retention.sweep_uploads(cfg=cfg)
        assert not folder.exists()
        assert swept.dirs == 1

    def test_a_folder_with_a_survivor_is_kept(self, cfg, user):
        folder = cfg.uploads_dir_for(user["user_id"])
        _aged(folder / "up_old.txt", days=40)
        kept = _aged(folder / "up_new.txt", days=1)
        swept = retention.sweep_uploads(cfg=cfg)
        assert folder.is_dir() and kept.exists()
        assert swept.files == 1 and swept.dirs == 0

    def test_a_missing_uploads_tree_is_not_an_error(self, cfg):
        """A fresh deployment, or one where nobody has uploaded anything."""
        import shutil

        shutil.rmtree(cfg.uploads_dir, ignore_errors=True)
        swept = retention.sweep_uploads(cfg=cfg)
        assert swept.files == 0 and swept.failed == []

    def test_a_loose_file_at_the_root_is_left_alone(self, cfg):
        """Nothing Brain writes lands there, so it is not Brain's to delete."""
        stray = _aged(cfg.uploads_dir / "something_else.txt", days=400)
        retention.sweep_uploads(cfg=cfg)
        assert stray.exists()

    def test_an_undeletable_file_is_reported_not_raised(self, cfg, user, monkeypatch):
        """Housekeeping must not be able to take the queue down."""
        folder = cfg.uploads_dir_for(user["user_id"])
        doomed = _aged(folder / "up_old.txt", days=40)
        survivor = _aged(folder / "up_older.txt", days=50)

        real = Path.unlink

        def refuse(self, *args, **kwargs):
            if self.name == "up_old.txt":
                raise PermissionError("read-only filesystem")
            return real(self, *args, **kwargs)

        monkeypatch.setattr(Path, "unlink", refuse)
        swept = retention.sweep_uploads(cfg=cfg)
        assert swept.failed == ["up_old.txt"]
        assert swept.files == 1, "the rest of the sweep carried on"
        assert doomed.exists() and not survivor.exists()
        assert folder.is_dir(), "a folder that still holds a file is kept"


class TestTheSweeperSchedule:
    def test_it_runs_on_the_first_call(self, cfg, user):
        """A container that has just booted sweeps once, which is what covers
        a deployment that ran before retention existed."""
        upload = _aged(cfg.uploads_dir_for(user["user_id"]) / "up_old.txt", days=40)
        sweeper = retention.Sweeper(cfg=cfg)
        assert sweeper.run_if_due() is not None
        assert not upload.exists()

    def test_then_at_most_once_an_hour(self, cfg):
        """The worker's idle loop ticks every two seconds; it must not stat
        the whole uploads tree each time."""
        sweeper = retention.Sweeper(cfg=cfg, interval=3600.0)
        start = time.time()
        assert sweeper.run_if_due(now=start) is not None
        assert sweeper.run_if_due(now=start + 60) is None
        assert sweeper.run_if_due(now=start + 3599) is None
        assert sweeper.run_if_due(now=start + 3601) is not None

    def test_the_worker_carries_one(self, cfg):
        """Hung on the worker because that is the one role both deployments
        run — in-process under `BRAIN_ROLE=web`, standalone under `=worker`."""
        from app.worker import Worker

        worker = Worker(cfg=cfg)
        assert isinstance(worker.sweeper, retention.Sweeper)

    def test_the_worker_swallows_a_sweep_failure(self, cfg, monkeypatch):
        from app.worker import Worker

        worker = Worker(cfg=cfg)
        monkeypatch.setattr(
            worker.sweeper, "run_if_due", lambda **_: (_ for _ in ()).throw(OSError("disk"))
        )
        assert worker.sweep_if_due() is None  # logged, not raised

    def test_the_loop_sweeps_before_it_claims(self):
        """Not a behaviour test — a wiring one. A sweep hung off the *idle*
        branch would never run on a container whose queue never empties."""
        source = (Path(__file__).resolve().parents[1] / "app/worker.py").read_text()
        run = source[source.index("async def run(") :]
        assert "sweep_if_due" in run[: run.index("try:")]

    @pytest.mark.parametrize(
        "role, inline", [("web", True), ("worker", False)]
    )
    def test_exactly_one_worker_runs_per_container(self, cfg, monkeypatch, role, inline):
        """Both production roles reach the sweep, by different routes: under
        `=web` the web process drains in-process, under `=worker` a dedicated
        process does. Either way a `Worker` exists, and `Worker` owns the
        sweeper — so there is no container where retention goes unenforced,
        and none where two sweepers contend."""
        from dataclasses import replace

        from app.main import inline_worker_enabled

        monkeypatch.setenv("BRAIN_ROLE", role)
        monkeypatch.delenv("BRAIN_INLINE_WORKER", raising=False)
        # `is_test` forces it off; production is what is being asserted.
        assert inline_worker_enabled(replace(cfg, env="prod")) is inline


class TestTheUsageScreenSaysNothingAboutStorage:
    def test_no_storage_key_and_no_byte_meter(self, cfg, user):
        """The founder's first ruling. Absent, not capless: a figure with no
        limit still invites the question of what the limit should be."""
        ctx = E.build_context(user["user_id"], surface="web_ui")
        usage = E.op_activity(ctx).data["usage"]
        assert "storage" not in usage
        assert set(usage) == {"sources", "credits", "calls"}
        for key, meter in usage.items():
            # Not a ban on byte-valued meters for all time — a ban on the one
            # the ruling was about. A future byte meter would have to argue
            # with `R-17` to get past this line, which is the point.
            assert meter["unit"] != "bytes", f"{key} reports bytes"
        # `R-18` replaced the minutes meter with credits; storage stayed out.
        assert usage["credits"]["unit"] == "credits"

    def test_the_engine_no_longer_stats_the_tenant_database(self):
        """It used to report `path.stat().st_size`. If that returns, so does
        the number the founder struck."""
        source = (Path(__file__).resolve().parents[1] / "app/core/engine.py").read_text()
        activity = source[source.index("def op_activity(") : source.index("_REMEDY_FOR = {")]
        assert "st_size" not in activity
