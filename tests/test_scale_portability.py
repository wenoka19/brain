"""Phase 7 — the scale-ready portability layer.

Two mechanisms, tested here without a network:

1. **Tenant snapshots to object storage** (`app/storage/r2_sync.py`) — every
   tenant database is a self-contained file in R2, pushed after a write and
   pulled on demand. That is what makes "move to Hetzner" or "add a Fly shard"
   a config change: a node needs no local state it cannot fetch.
2. **Stateless shard routing** (`app/main.py`) — every node derives the owner
   of a tenant from `crc32(user_id) % shard_total`, so there is no router to
   keep in sync.

Every R2 call goes through an `httpx.MockTransport` that keeps objects in a
dict, so the suite exercises the real signing, the real `VACUUM INTO`, and the
real atomic rename — just not a real bucket.

What these tests cannot prove: that the SigV4 signature is one Cloudflare
accepts. `botocore` is not installed here, so there is no reference
implementation to cross-check against. They prove the canonical request has
the documented shape, that the signature is deterministic and changes with
every input it should, and that the headers are well formed. The first live
PUT is still the real verification.
"""

from __future__ import annotations

import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest
from starlette.testclient import TestClient

from app import mcp_server as M
from app import webapi
from app.config import Settings, load_settings
from app.core import engine as E
from app.db import control as control_db
from app.db import tenant as tenant_db
from app.main import create_app
from app.storage import r2_sync

R2 = r2_sync.R2Config(
    endpoint_url="https://acct.r2.cloudflarestorage.com",
    bucket="brain-tenants",
    access_key_id="AKIAEXAMPLE",
    secret_access_key="s3cret-example-key",
    region="auto",
    prefix="tenants/",
)


# ---------------------------------------------------------------------------
# a fake bucket
# ---------------------------------------------------------------------------
class FakeBucket:
    """An in-memory S3 that records what it was asked, and how."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.calls: list[tuple[str, str]] = []
        self.requests: list[httpx.Request] = []
        self.fail_put_with: int | None = None

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        key = request.url.path.split(f"/{R2.bucket}/", 1)[-1]
        self.calls.append((request.method, key))

        if request.method == "PUT":
            if self.fail_put_with:
                return httpx.Response(self.fail_put_with, text="nope")
            self.objects[key] = request.content
            return httpx.Response(200, headers={"ETag": '"fake"'})
        if key not in self.objects:
            return httpx.Response(404, text="NoSuchKey")
        body = self.objects[key]
        if request.method == "HEAD":
            return httpx.Response(200, headers={"content-length": str(len(body))})
        return httpx.Response(200, content=body)

    def install(self) -> None:
        r2_sync.set_config(R2)
        r2_sync.set_http_client_factory(
            lambda: httpx.Client(transport=httpx.MockTransport(self.handler))
        )

    def keys(self) -> list[str]:
        return sorted(self.objects)

    def methods(self) -> list[str]:
        return [method for method, _ in self.calls]


@pytest.fixture
def bucket(cfg):
    fake = FakeBucket()
    fake.install()
    return fake


def claim_count(path: Path) -> int:
    """Read a raw snapshot file the way a *different machine* would."""
    conn = sqlite3.connect(str(path))
    try:
        return conn.execute("SELECT count(*) FROM epistemic_claims").fetchone()[0]
    finally:
        conn.close()


def remember(ctx: E.Context, text: str) -> E.OpResult:
    return E.op_remember_this(
        ctx, claim=text, when_it_applies="When testing the snapshot path."
    )


# ===========================================================================
# SigV4
# ===========================================================================
class TestSignatureV4:
    def test_the_canonical_request_has_the_documented_shape(self):
        headers = {
            "host": "acct.r2.cloudflarestorage.com",
            "x-amz-date": "20261028T120000Z",
            "x-amz-content-sha256": "abc",
        }
        canonical = r2_sync.canonical_request(
            "PUT", "/brain-tenants/tenants/user_1.db", "abc", headers
        )
        lines = canonical.split("\n")
        assert lines[0] == "PUT"
        assert lines[1] == "/brain-tenants/tenants/user_1.db"
        assert lines[2] == ""  # no query string
        # Headers are lowercased, sorted, newline-terminated, then the signed
        # list, then the payload hash. Any drift here is a 403 from R2.
        assert lines[3:6] == [
            "host:acct.r2.cloudflarestorage.com",
            "x-amz-content-sha256:abc",
            "x-amz-date:20261028T120000Z",
        ]
        assert lines[6] == ""
        assert lines[7] == "host;x-amz-content-sha256;x-amz-date"
        assert lines[8] == "abc"

    def test_signing_is_deterministic(self):
        moment = datetime(2026, 10, 28, 12, 0, tzinfo=timezone.utc)
        kwargs = dict(
            method="PUT",
            url=R2.url_for("tenants/user_1.db"),
            payload_hash="deadbeef",
            cfg=R2,
            now=moment,
        )
        first = r2_sync.sigv4_headers(**kwargs)
        second = r2_sync.sigv4_headers(**kwargs)
        assert first == second
        assert first["x-amz-date"] == "20261028T120000Z"
        assert first["Authorization"].startswith("AWS4-HMAC-SHA256 Credential=AKIAEXAMPLE/20261028/auto/s3/aws4_request")
        assert "SignedHeaders=host;x-amz-content-sha256;x-amz-date" in first["Authorization"]
        assert len(first["Authorization"].split("Signature=")[1]) == 64

    @pytest.mark.parametrize(
        "change",
        [
            {"method": "GET"},
            {"payload_hash": "cafe"},
            {"url": R2.url_for("tenants/user_2.db")},
            {"cfg": R2.__class__(**{**R2.__dict__, "secret_access_key": "other"})},
            {"cfg": R2.__class__(**{**R2.__dict__, "region": "wnam"})},
            {"now": datetime(2026, 10, 29, 12, 0, tzinfo=timezone.utc)},
        ],
    )
    def test_every_signed_input_changes_the_signature(self, change):
        base = dict(
            method="PUT",
            url=R2.url_for("tenants/user_1.db"),
            payload_hash="deadbeef",
            cfg=R2,
            now=datetime(2026, 10, 28, 12, 0, tzinfo=timezone.utc),
        )
        original = r2_sync.sigv4_headers(**base)["Authorization"]
        altered = r2_sync.sigv4_headers(**{**base, **change})["Authorization"]
        assert original != altered

    def test_urls_are_path_style_because_r2_has_no_virtual_hosted_buckets(self):
        assert (
            R2.url_for("tenants/user_abc.db")
            == "https://acct.r2.cloudflarestorage.com/brain-tenants/tenants/user_abc.db"
        )
        assert R2.key_for("abc") == "tenants/user_abc.db"

    def test_a_put_signs_the_body_it_actually_sends(self, bucket, tmp_path):
        payload = tmp_path / "blob.db"
        payload.write_bytes(b"sqlite-ish bytes")
        assert r2_sync.put_object("tenants/user_x.db", payload) is True

        request = bucket.requests[-1]
        assert request.headers["x-amz-content-sha256"] == r2_sync.sha256_file(payload)
        assert "Signature=" in request.headers["authorization"]
        assert bucket.objects["tenants/user_x.db"] == b"sqlite-ish bytes"


# ===========================================================================
# object storage plumbing
# ===========================================================================
class TestObjectStore:
    def test_a_missing_object_is_absent_not_an_error(self, bucket, tmp_path):
        assert r2_sync.head_object("tenants/user_nope.db") is None
        assert r2_sync.get_object("tenants/user_nope.db", tmp_path / "out.db") is False
        assert not (tmp_path / "out.db").exists()

    def test_a_download_lands_atomically(self, bucket, tmp_path):
        bucket.objects["tenants/user_a.db"] = b"x" * 2048
        destination = tmp_path / "nested" / "user_a.db"
        assert r2_sync.get_object("tenants/user_a.db", destination) is True
        assert destination.read_bytes() == b"x" * 2048
        # The staging file is renamed, never left behind: a half-written
        # database must not be openable under the real name.
        assert list(destination.parent.glob("*.hydrating")) == []

    def test_everything_is_a_no_op_when_r2_is_unconfigured(self, cfg, tmp_path):
        r2_sync.set_config(r2_sync.R2Config())
        calls: list[str] = []
        r2_sync.set_http_client_factory(lambda: calls.append("client") or httpx.Client())

        assert r2_sync.enabled() is False
        assert r2_sync.head_object("tenants/user_a.db") is None
        assert r2_sync.get_object("tenants/user_a.db", tmp_path / "a.db") is False
        assert r2_sync.put_object("tenants/user_a.db", tmp_path) is False
        assert r2_sync.hydrate("a", tmp_path / "a.db") is False
        assert calls == [], "an unconfigured deployment must not open a socket"

    def test_a_server_error_is_reported_not_raised(self, bucket, tmp_path):
        bucket.fail_put_with = 500
        payload = tmp_path / "blob.db"
        payload.write_bytes(b"bytes")
        assert r2_sync.put_object("tenants/user_x.db", payload) is False


# ===========================================================================
# lazy hydration
# ===========================================================================
class TestLazyHydration:
    def test_a_cold_node_pulls_the_tenant_it_has_never_seen(self, cfg, bucket, user):
        """The migration story in one test: the file is gone from this disk and
        the user still has their library."""
        user_id = user["user_id"]
        ctx = E.build_context(user_id, surface="web_ui")
        assert remember(ctx, "Hydration proves portability.").ok

        # Snapshot it to the bucket the way the background thread would, then
        # wipe the node — a new Fly machine, or a fresh Hetzner box.
        assert r2_sync.snapshot_and_upload(user_id, cfg=cfg) is True
        path = cfg.tenant_db_path(user_id)
        tenant_db.close_tenant(user_id, cfg=cfg)
        for suffix in ("", "-wal", "-shm"):
            Path(str(path) + suffix).unlink(missing_ok=True)
        assert not path.exists()
        bucket.calls.clear()

        conn = tenant_db.tenant(user_id, cfg=cfg)

        assert ("GET", "tenants/" + f"user_{user_id}.db") in bucket.calls
        assert path.exists()
        assert conn.execute(
            "SELECT count(*) FROM epistemic_claims WHERE claim LIKE '%portability%'"
        ).fetchone()[0] == 1

    def test_a_tenant_already_on_disk_is_never_downloaded(self, cfg, bucket, user):
        user_id = user["user_id"]
        tenant_db.tenant(user_id, cfg=cfg)  # creates the file
        bucket.calls.clear()
        tenant_db.close_tenant(user_id, cfg=cfg)

        tenant_db.tenant(user_id, cfg=cfg)
        assert bucket.calls == [], "hydration must not cost a round trip per open"

    def test_a_tenant_in_neither_place_gets_a_fresh_schema(self, cfg, bucket, user):
        """Absent locally *and* absent in R2 is a new user, not an error."""
        user_id = user["user_id"]
        conn = tenant_db.tenant(user_id, cfg=cfg)
        assert ("GET", f"tenants/user_{user_id}.db") in bucket.calls
        assert conn.execute("SELECT count(*) FROM epistemic_claims").fetchone()[0] == 0
        assert remember(E.build_context(user_id), "A fresh tenant still works.").ok


# ===========================================================================
# post-write snapshots
# ===========================================================================
class TestPostWriteSnapshot:
    def test_a_write_marks_the_tenant_and_a_flush_uploads_a_usable_database(
        self, cfg, bucket, user
    ):
        user_id = user["user_id"]
        ctx = E.build_context(user_id)
        assert r2_sync.is_dirty(user_id) is False

        assert remember(ctx, "Snapshots follow writes, not a clock.").ok
        assert r2_sync.is_dirty(user_id) is True
        assert bucket.methods().count("PUT") == 0, "the write itself must not block on R2"

        flushed = r2_sync.flush_all(cfg=cfg)

        assert flushed == [user_id]
        assert bucket.keys() == [f"tenants/user_{user_id}.db"]
        assert r2_sync.is_dirty(user_id) is False
        # What landed must be a complete, openable SQLite database — that is
        # the whole point of `VACUUM INTO` over copying a file with a live WAL.
        landed = Path(cfg.data_dir) / "landed.db"
        landed.write_bytes(bucket.objects[f"tenants/user_{user_id}.db"])
        assert claim_count(landed) == 1

    def test_the_snapshot_leaves_no_temporary_file_behind(self, cfg, bucket, user):
        user_id = user["user_id"]
        assert remember(E.build_context(user_id), "Clean up after yourself.").ok
        r2_sync.flush_all(cfg=cfg)
        assert list(Path(cfg.snapshot_dir).glob("*.snap.db")) == []

    def test_the_debounce_coalesces_a_burst_into_one_upload(self, cfg, bucket, user):
        """Ten claims in a chat turn should cost one PUT, not ten."""
        user_id = user["user_id"]
        ctx = E.build_context(user_id)
        marked = time.monotonic()
        for index in range(5):
            assert remember(ctx, f"Burst claim number {index} about debouncing.").ok

        assert r2_sync.flush_due(cfg=cfg, now=marked) == []
        assert bucket.methods().count("PUT") == 0

        done = r2_sync.flush_due(cfg=cfg, now=marked + cfg.snapshot_debounce_sec + 0.1)
        assert done == [user_id]
        assert bucket.methods().count("PUT") == 1

    def test_a_failed_upload_keeps_the_tenant_dirty(self, cfg, bucket, user):
        """Losing a write silently is the one unacceptable outcome."""
        user_id = user["user_id"]
        assert remember(E.build_context(user_id), "Retry rather than forget.").ok
        bucket.fail_put_with = 503

        assert r2_sync.flush_all(cfg=cfg) == []
        assert r2_sync.is_dirty(user_id) is True

        bucket.fail_put_with = None
        assert r2_sync.flush_all(cfg=cfg) == [user_id]
        assert r2_sync.is_dirty(user_id) is False

    def test_an_ingestion_marks_the_tenant(self, cfg, bucket, user):
        user_id = user["user_id"]
        ctx = E.build_context(user_id)
        assert E.op_ingest_source(ctx, "https://www.youtube.com/watch?v=Portab1lity").ok
        r2_sync.reset_state()  # isolate the pipeline's own marking

        E.run_pending_jobs_sync(user_id, cfg=cfg)

        assert r2_sync.is_dirty(user_id) is True

    def test_nothing_is_uploaded_when_r2_is_unconfigured(self, cfg, user):
        r2_sync.set_config(r2_sync.R2Config())
        assert remember(E.build_context(user["user_id"]), "Local-only still works.").ok
        assert r2_sync.flush_all(cfg=cfg) == []


# ===========================================================================
# LRU disk eviction
# ===========================================================================
class TestLocalDiskGuard:
    def _tenant_with_a_claim(self, cfg, control, email: str) -> str:
        record = control_db.ensure_user(control, email)
        user_id = record["user_id"]
        assert remember(E.build_context(user_id, cfg=cfg), f"A claim for {email}.").ok
        return user_id

    def test_eviction_is_off_by_default(self, cfg, bucket, control):
        self._tenant_with_a_claim(cfg, control, "a@example.com")
        self._tenant_with_a_claim(cfg, control, "b@example.com")
        r2_sync.flush_all(cfg=cfg)
        assert cfg.max_local_tenants == 0
        assert r2_sync.maybe_evict(cfg=cfg) == []
        assert len(r2_sync.local_tenant_files(cfg)) == 2

    def test_the_least_recently_used_synced_tenant_is_dropped(
        self, cfg, bucket, control, monkeypatch
    ):
        monkeypatch.setenv("MAX_LOCAL_TENANTS", "1")
        capped = load_settings()

        old = self._tenant_with_a_claim(capped, control, "old@example.com")
        recent = self._tenant_with_a_claim(capped, control, "recent@example.com")
        r2_sync.flush_all(cfg=capped)
        r2_sync.mark_accessed(old, now=100.0)
        r2_sync.mark_accessed(recent, now=200.0)

        assert r2_sync.maybe_evict(cfg=capped) == [old]
        assert not capped.tenant_db_path(old).exists()
        assert capped.tenant_db_path(recent).exists()

        # And the evicted user is not harmed: their next request hydrates.
        conn = tenant_db.tenant(old, cfg=capped)
        assert conn.execute("SELECT count(*) FROM epistemic_claims").fetchone()[0] == 1

    def test_an_unsynced_tenant_is_never_evicted(self, cfg, bucket, control, monkeypatch):
        """The guard may cost a download. It may never cost a write."""
        monkeypatch.setenv("MAX_LOCAL_TENANTS", "1")
        capped = load_settings()

        unsynced = self._tenant_with_a_claim(capped, control, "dirty@example.com")
        synced = self._tenant_with_a_claim(capped, control, "clean@example.com")
        assert r2_sync.snapshot_and_upload(synced, cfg=capped) is True
        r2_sync.mark_accessed(unsynced, now=100.0)  # the better eviction candidate
        r2_sync.mark_accessed(synced, now=200.0)

        assert r2_sync.is_dirty(unsynced) is True
        assert r2_sync.maybe_evict(cfg=capped) == [synced]
        assert capped.tenant_db_path(unsynced).exists()

    def test_eviction_is_off_without_object_storage(self, cfg, control, monkeypatch):
        """Without R2 an evicted file is simply gone, so the cap is ignored."""
        monkeypatch.setenv("MAX_LOCAL_TENANTS", "1")
        capped = load_settings()
        r2_sync.set_config(r2_sync.R2Config())
        self._tenant_with_a_claim(capped, control, "a@example.com")
        self._tenant_with_a_claim(capped, control, "b@example.com")
        assert r2_sync.maybe_evict(cfg=capped) == []
        assert len(r2_sync.local_tenant_files(capped)) == 2


# ===========================================================================
# shard routing
# ===========================================================================
class TestShardMath:
    def test_a_single_node_owns_everything(self, cfg):
        assert cfg.sharded is False
        assert all(cfg.owns_shard(f"user{i}") for i in range(50))

    def test_ownership_is_stable_and_spread(self, monkeypatch):
        monkeypatch.setenv("BRAIN_SHARD_TOTAL", "4")
        cfg = load_settings()
        ids = [f"user{index:04d}" for index in range(400)]
        first = [cfg.shard_for(user_id) for user_id in ids]

        # Stable: the same answer every time, in every process. crc32 is chosen
        # over hash() precisely because hash() is randomised per interpreter.
        assert first == [cfg.shard_for(user_id) for user_id in ids]
        assert set(first) == {0, 1, 2, 3}
        # Roughly even, or one shard carries the load for all of them.
        for shard in range(4):
            assert 60 <= first.count(shard) <= 140

    def test_an_out_of_range_index_falls_back_to_zero(self, monkeypatch):
        monkeypatch.setenv("BRAIN_SHARD_TOTAL", "2")
        monkeypatch.setenv("BRAIN_SHARD_INDEX", "7")
        assert load_settings().shard_index == 0

    def test_addresses_prefer_a_fly_instance_over_a_url(self, monkeypatch):
        monkeypatch.setenv("BRAIN_SHARD_TOTAL", "2")
        monkeypatch.setenv("BRAIN_SHARD_URLS", "https://s0.example,https://s1.example/")
        monkeypatch.setenv("BRAIN_SHARD_INSTANCES", "aaaa,bbbb")
        cfg = load_settings()
        assert cfg.shard_address(1) == ("instance", "bbbb")
        monkeypatch.delenv("BRAIN_SHARD_INSTANCES")
        assert load_settings().shard_address(1) == ("url", "https://s1.example")
        monkeypatch.delenv("BRAIN_SHARD_URLS")
        assert load_settings().shard_address(1) == ("", "")


SHARD_SECRET = "shard-test-secret-key-long-enough"


def owner_of(user_id: str, total: int) -> int:
    """Which shard would own this tenant in a `total`-shard deployment."""
    return Settings(shard_total=total).shard_for(user_id)


def shard_client(cfg, monkeypatch, *, index: int, total: int, **env: str):
    monkeypatch.setenv("BRAIN_JWT_SECRET", SHARD_SECRET)
    monkeypatch.setenv("BRAIN_SHARD_INDEX", str(index))
    monkeypatch.setenv("BRAIN_SHARD_TOTAL", str(total))
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    sharded = load_settings()
    sharded.ensure_dirs()
    M.reset_server()
    return sharded, create_app(sharded)


class TestShardRouting:
    """The middleware is wired in `create_app`, so these go through the app."""

    def _session(self, cfg, monkeypatch, email: str = "founder@example.com") -> tuple[str, str]:
        # The cookie is HMAC'd with the JWT secret, so it has to be the one the
        # app will later verify with — set it before minting, not after.
        monkeypatch.setenv("BRAIN_JWT_SECRET", SHARD_SECRET)
        record = control_db.ensure_user(control_db.control(), email)
        return record["user_id"], webapi.issue_session_cookie(record["user_id"], cfg=cfg)

    def test_a_request_for_another_shard_is_redirected(self, cfg, monkeypatch):
        user_id, cookie = self._session(cfg, monkeypatch)
        owner = owner_of(user_id, 2)  # whichever that turns out to be
        sharded, app = shard_client(
            cfg,
            monkeypatch,
            index=(owner + 1) % 2,
            total=2,
            BRAIN_SHARD_URLS="https://s0.example,https://s1.example",
        )
        assert sharded.owns_shard(user_id) is False

        with TestClient(app) as client:
            response = client.get(
                "/api/cards?view=everything",
                cookies={webapi.SESSION_COOKIE: cookie},
                follow_redirects=False,
            )

        assert response.status_code == 307
        assert response.headers["location"] == (
            f"https://s{owner}.example/api/cards?view=everything"
        )
        M.reset_server()

    def test_fly_gets_a_replay_header_instead_of_a_redirect(self, cfg, monkeypatch):
        user_id, cookie = self._session(cfg, monkeypatch)
        owner = owner_of(user_id, 2)
        sharded, app = shard_client(
            cfg,
            monkeypatch,
            index=(owner + 1) % 2,
            total=2,
            BRAIN_SHARD_INSTANCES="machine-zero,machine-one",
        )

        with TestClient(app) as client:
            response = client.get(
                "/api/cards", cookies={webapi.SESSION_COOKIE: cookie}, follow_redirects=False
            )

        assert response.status_code == 204
        assert response.headers["fly-replay"] == (
            f"instance=machine-{'zero' if owner == 0 else 'one'}"
        )
        M.reset_server()

    def test_the_owning_shard_serves_the_request_itself(self, cfg, monkeypatch):
        user_id, cookie = self._session(cfg, monkeypatch)
        owner = owner_of(user_id, 2)
        sharded, app = shard_client(
            cfg,
            monkeypatch,
            index=owner,
            total=2,
            BRAIN_SHARD_URLS="https://s0.example,https://s1.example",
        )

        with TestClient(app) as client:
            response = client.get("/api/cards", cookies={webapi.SESSION_COOKIE: cookie})

        assert response.status_code == 200
        assert "location" not in response.headers
        M.reset_server()

    def test_discovery_and_health_are_served_by_any_node(self, cfg, monkeypatch):
        user_id, cookie = self._session(cfg, monkeypatch)
        owner = owner_of(user_id, 2)
        sharded, app = shard_client(
            cfg,
            monkeypatch,
            index=(owner + 1) % 2,
            total=2,
            BRAIN_SHARD_URLS="https://s0.example,https://s1.example",
        )

        with TestClient(app) as client:
            for path in ("/healthz", "/.well-known/oauth-protected-resource"):
                response = client.get(
                    path, cookies={webapi.SESSION_COOKIE: cookie}, follow_redirects=False
                )
                assert response.status_code == 200, path
        M.reset_server()

    def test_an_anonymous_request_is_never_routed(self, cfg, monkeypatch):
        sharded, app = shard_client(
            cfg, monkeypatch, index=0, total=2, BRAIN_SHARD_URLS="https://s0,https://s1"
        )
        with TestClient(app) as client:
            response = client.get("/api/cards", follow_redirects=False)
        # No identity, so no owner to compute: it is answered here, as a 401.
        assert response.status_code == 401
        M.reset_server()

    def test_a_forged_cookie_cannot_steer_routing(self, cfg, monkeypatch):
        """Identity is verified before it is used, so routing is not an oracle
        and not a way to be served by a node that does not own you."""
        sharded, app = shard_client(
            cfg, monkeypatch, index=0, total=2, BRAIN_SHARD_URLS="https://s0,https://s1"
        )
        with TestClient(app) as client:
            response = client.get(
                "/api/cards",
                cookies={webapi.SESSION_COOKIE: "eyJzdWIiOiAiemVicmEifQ.forged"},
                follow_redirects=False,
            )
        assert response.status_code == 401
        M.reset_server()

    def test_a_single_shard_deployment_adds_no_routing(self, cfg, monkeypatch):
        user_id, cookie = self._session(cfg, monkeypatch)
        sharded, app = shard_client(cfg, monkeypatch, index=0, total=1)
        assert sharded.sharded is False
        with TestClient(app) as client:
            assert client.get("/api/cards", cookies={webapi.SESSION_COOKIE: cookie}).status_code == 200
        M.reset_server()


# ===========================================================================
# the operational contract
# ===========================================================================
class TestDeploymentContract:
    def test_litestream_is_not_asked_to_watch_tenants(self):
        """Duplicated deliberately from the Phase 5 suite: this is the one
        invariant that keeps 10,000 tenants from exhausting inotify."""
        text = Path("litestream.yml").read_text()
        assert "control.db" in text
        assert "user_" not in text.split("dbs:")[1]

    def test_the_background_thread_only_runs_when_it_has_somewhere_to_push(self, cfg):
        r2_sync.set_config(r2_sync.R2Config())
        assert r2_sync.start_background(cfg=cfg) is False

    def test_the_background_thread_flushes_on_shutdown(self, cfg, bucket, user):
        user_id = user["user_id"]
        assert r2_sync.start_background(cfg=cfg) is True
        try:
            assert remember(E.build_context(user_id), "A write during shutdown.").ok
        finally:
            r2_sync.stop_background(cfg=cfg)
        assert bucket.keys() == [f"tenants/user_{user_id}.db"]
        assert r2_sync.is_dirty(user_id) is False
