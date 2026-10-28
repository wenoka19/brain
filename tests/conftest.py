"""Shared fixtures. Every test runs in `BRAIN_ENV=test` against a throwaway
data directory, so nothing touches the network, `/data`, or another test."""

from __future__ import annotations

import math
import random

import pytest

from app import config
from app.db import control as control_db
from app.db import tenant as tenant_db
from app.security import ratelimit


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("BRAIN_ENV", "test")
    monkeypatch.setenv("BRAIN_DATA_DIR", str(tmp_path))
    settings = config.reload_settings()
    settings.ensure_dirs()
    ratelimit.reset_limiter()
    yield settings
    tenant_db.close_all()
    control_db.close_all()
    ratelimit.reset_limiter()
    config.reload_settings()


@pytest.fixture
def control(cfg):
    return control_db.control()


@pytest.fixture
def user(control):
    return control_db.ensure_user(control, "founder@example.com")


@pytest.fixture
def tdb(cfg, user):
    return tenant_db.tenant(user["user_id"])


def unit_vector(seed: int, dim: int = 256) -> list[float]:
    """Deterministic L2-normalised vector, for tests that only need *a* vector."""
    rng = random.Random(seed)
    values = [rng.random() - 0.5 for _ in range(dim)]
    norm = math.sqrt(sum(v * v for v in values)) or 1.0
    return [v / norm for v in values]


@pytest.fixture
def vec():
    return unit_vector
