"""Tests for the scoring API: routes, schemas, service, and HTTP mapping.

Uses FastAPI's TestClient with a dependency override so each test gets a
self-contained Settings pointing at tmp fixtures (indicators parquet +
weights.yaml), not the repo's real artifacts.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from fastapi.testclient import TestClient

from nyc_apartments_map.api.app import create_app
from nyc_apartments_map.config import Settings, get_settings

#: A 4-sub-score profile matching CompositeWeightsRequest's fixed fields.
_FULL_WEIGHTS = """
composite:
  affordability: 0.3
  safety: 0.3
  quality: 0.2
  amenity: 0.2
affordability:
  median_price: {weight: 1.0, direction: lower}
safety:
  felony_per_1k_units: {weight: 1.0, direction: lower}
quality:
  hpd_violation_per_1k_units: {weight: 1.0, direction: lower}
amenity:
  nightlife_per_1k_units: {weight: 1.0, direction: higher}
"""

#: A 2-sub-score profile (used to exercise request/profile key mismatch).
_PARTIAL_WEIGHTS = """
composite:
  affordability: 0.5
  safety: 0.5
affordability:
  median_price: {weight: 1.0, direction: lower}
safety:
  felony_per_1k_units: {weight: 1.0, direction: lower}
"""


def _indicators_df() -> pd.DataFrame:
    """3 residential NTAs + 1 park, with raw counts so rates re-derive cleanly.

    pluto_res_units=1000 for residential NTAs so per-1k rate == raw count.
    """
    return pd.DataFrame(
        {
            "nta_code": ["N1", "N2", "N3", "PARK"],
            "nta_name": ["N1", "N2", "N3", "Park"],
            "nta_type": ["0", "0", "0", "9"],
            "cdta_code": ["X", "X", "X", "X"],
            "cdta_name": ["X", "X", "X", "X"],
            "median_price": [2000.0, 3000.0, 4000.0, np.nan],
            "nypd_felony_count": [10, 20, 30, 0],
            "hpd_violation_count": [1, 2, 3, 0],
            "nightlife_venue_count": [5, 10, 15, 0],
            "pluto_res_units": [1000, 1000, 1000, 0],
        }
    )


def _write_parquet(df: pd.DataFrame, path: object) -> None:
    table = pa.Table.from_pandas(df, preserve_index=False)
    pq.write_table(table, str(path))


def _make_app(
    tmp_path: object,
    weights_text: str,
    *,
    parquet_exists: bool = True,
) -> TestClient:
    """Build an app wired to tmp fixtures via a get_settings override.

    Writes weights.yaml + (optionally) the indicators parquet under tmp_path,
    constructs Settings pointing at them, and overrides the route's
    ``get_settings`` dependency so requests use the test settings.
    """
    import pathlib

    tmp = pathlib.Path(tmp_path)
    weights_path = tmp / "weights.yaml"
    weights_path.write_text(weights_text)
    indicators_path = tmp / "nta_indicators.parquet"
    if parquet_exists:
        _write_parquet(_indicators_df(), indicators_path)

    settings = Settings(project_root=tmp)
    settings.weights_path = weights_path
    settings.nta_indicators_path = indicators_path
    settings.maps_dir = tmp / "maps"
    settings.maps_dir.mkdir(parents=True, exist_ok=True)

    app = create_app(settings)
    app.dependency_overrides[get_settings] = lambda: settings
    return TestClient(app)


# --- POST /api/scores: valid request ----------------------------------------


def test_post_scores_affordability_only_matches_handcomputed(tmp_path) -> None:
    """Override to 100% affordability -> composite == affordability_score * 100.

    median_price [2000,3000,4000] over 3 residential NTAs, direction=lower:
    rank(pct)=[1/3,2/3,1.0] flipped -> [2/3,1/3,0.0]; *100 -> [66.67,33.33,0].
    """
    client = _make_app(tmp_path, _FULL_WEIGHTS)
    resp = client.post(
        "/api/scores",
        json={"affordability": 1, "safety": 0, "quality": 0, "amenity": 0},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["metric"] == "desirability_score"
    assert set(body["scores"]) == {"N1", "N2", "N3", "PARK"}

    n1, n2, n3, park = (body["scores"][k] for k in ("N1", "N2", "N3", "PARK"))
    assert n1["desirability_score"] == pytest.approx(100 * 2 / 3, abs=1e-2)
    assert n2["desirability_score"] == pytest.approx(100 * 1 / 3, abs=1e-2)
    assert n3["desirability_score"] == pytest.approx(0.0, abs=1e-2)
    # Park is non-residential -> no score, gray fill.
    assert park["desirability_score"] is None
    assert park["color"] == "#cccccc"

    # Sub-scores carried through; affordability populated, others may be present too.
    assert n1["sub_scores"]["affordability_score"] == pytest.approx(2 / 3, abs=1e-4)


def test_post_scores_normalizes_non_summing_weights(tmp_path) -> None:
    """Weights need not sum to 1: {affordability:2, others:0} == {1,0,0,0}."""
    client = _make_app(tmp_path, _FULL_WEIGHTS)
    normalized = client.post(
        "/api/scores", json={"affordability": 2, "safety": 0, "quality": 0, "amenity": 0}
    ).json()
    baseline = client.post(
        "/api/scores", json={"affordability": 1, "safety": 0, "quality": 0, "amenity": 0}
    ).json()
    for nta in ("N1", "N2", "N3"):
        assert normalized["scores"][nta]["desirability_score"] == pytest.approx(
            baseline["scores"][nta]["desirability_score"]
        )


def test_post_scores_color_is_hex_and_domain_present(tmp_path) -> None:
    client = _make_app(tmp_path, _FULL_WEIGHTS)
    body = client.post(
        "/api/scores", json={"affordability": 1, "safety": 0, "quality": 0, "amenity": 0}
    ).json()
    assert body["domain"]["lo"] < body["domain"]["hi"]
    for nta, data in body["scores"].items():
        if data["desirability_score"] is not None:
            assert data["color"].startswith("#") and len(data["color"]) == 7, nta


# --- POST /api/scores: validation 422s --------------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        {"affordability": -1, "safety": 1, "quality": 0, "amenity": 0},  # negative
        {"affordability": 0, "safety": 0, "quality": 0, "amenity": 0},  # all zero
        {"affordability": 1, "safety": 0, "quality": 0, "amenity": 0, "foo": 1},  # extra
    ],
)
def test_post_scores_rejects_invalid_payload(tmp_path, payload) -> None:
    client = _make_app(tmp_path, _FULL_WEIGHTS)
    resp = client.post("/api/scores", json=payload)
    assert resp.status_code == 422


def test_post_scores_profile_key_mismatch_is_422(tmp_path) -> None:
    """Request has all 4 fields but weights.yaml defines only 2 sub-scores."""
    client = _make_app(tmp_path, _PARTIAL_WEIGHTS)
    resp = client.post(
        "/api/scores", json={"affordability": 1, "safety": 0, "quality": 0, "amenity": 0}
    )
    assert resp.status_code == 422
    assert "do not match" in resp.json()["detail"]


# --- POST /api/scores: missing artifacts ------------------------------------


def test_post_scores_missing_parquet_is_503(tmp_path) -> None:
    client = _make_app(tmp_path, _FULL_WEIGHTS, parquet_exists=False)
    resp = client.post(
        "/api/scores", json={"affordability": 1, "safety": 0, "quality": 0, "amenity": 0}
    )
    assert resp.status_code == 503
    assert "indicators" in resp.json()["detail"].lower()


def test_post_scores_missing_weights_is_422(tmp_path) -> None:
    import pathlib

    tmp = pathlib.Path(tmp_path)
    settings = Settings(project_root=tmp)
    settings.weights_path = tmp / "absent.yaml"
    settings.nta_indicators_path = tmp / "absent.parquet"
    settings.maps_dir = tmp / "maps"
    settings.maps_dir.mkdir(parents=True, exist_ok=True)
    app = create_app(settings)
    app.dependency_overrides[get_settings] = lambda: settings
    client = TestClient(app)
    resp = client.post(
        "/api/scores", json={"affordability": 1, "safety": 0, "quality": 0, "amenity": 0}
    )
    assert resp.status_code == 422
    assert "weights" in resp.json()["detail"].lower()


# --- GET /api/weights --------------------------------------------------------


def test_get_weights_returns_profile(tmp_path) -> None:
    client = _make_app(tmp_path, _FULL_WEIGHTS)
    resp = client.get("/api/weights")
    assert resp.status_code == 200
    body = resp.json()
    assert body["composite"] == {
        "affordability": 0.3,
        "safety": 0.3,
        "quality": 0.2,
        "amenity": 0.2,
    }
    names = [s["name"] for s in body["sub_scores"]]
    assert names == ["affordability", "safety", "quality", "amenity"]
    # Spot-check one metric's direction echo.
    aff = next(s for s in body["sub_scores"] if s["name"] == "affordability")
    assert aff["metrics"][0] == {"name": "median_price", "weight": 1.0, "direction": "lower"}


def test_get_weights_missing_is_404(tmp_path) -> None:
    import pathlib

    tmp = pathlib.Path(tmp_path)
    settings = Settings(project_root=tmp)
    settings.weights_path = tmp / "absent.yaml"
    settings.maps_dir = tmp / "maps"
    settings.maps_dir.mkdir(parents=True, exist_ok=True)
    app = create_app(settings)
    app.dependency_overrides[get_settings] = lambda: settings
    client = TestClient(app)
    resp = client.get("/api/weights")
    assert resp.status_code == 404
