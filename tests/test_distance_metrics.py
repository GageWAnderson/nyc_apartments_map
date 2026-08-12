"""Tests for processing/distance_metrics.py: per-NTA Google Maps distance metrics."""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime, timedelta

import geopandas as gpd
import pytest
from shapely.geometry import Point, Polygon

_VALID_YAML = """
metrics:
  - name: distance_from_times_square
    destination: "Times Square, New York, NY"
    mode: transit
    departure_time: "next_monday_09:00"
    units: metric
"""

_MINIMAL_YAML = """
metrics:
  - name: distance_from_work
    destination: "1 Wall Street, Manhattan"
"""

_TWO_METRIC_YAML = """
metrics:
  - name: distance_from_times_square
    destination: "Times Square, New York, NY"
    mode: transit
  - name: distance_from_penn_station
    destination: "Penn Station, New York, NY"
    mode: walking
"""


def _two_nta_boundaries() -> gpd.GeoDataFrame:
    """Two unit-square NTA polygons: AA0101 at origin, BB0201 at x=2."""
    return gpd.GeoDataFrame(
        {
            "nta_code": ["AA0101", "BB0201"],
            "nta_name": ["Area A", "Area B"],
            "nta_type": ["0", "0"],
            "cdta_code": ["AA01", "BB02"],
            "cdta_name": ["CDTA A", "CDTA B"],
        },
        geometry=[
            Polygon([(0, 0), (0, 1), (1, 1), (1, 0)]),
            Polygon([(2, 0), (2, 1), (3, 1), (3, 0)]),
        ],
        crs="EPSG:4326",
    )


def _settings_with_config(tmp_path, yaml_content: str | None = None, *, with_key: bool = True):
    """Settings whose cache_dir + config_path point at tmp_path."""
    from nyc_apartments_map.config import Settings

    settings = Settings(project_root=tmp_path)
    settings.google_api_key = "test-key" if with_key else None
    settings.distance_cache_dir = tmp_path / "cache"
    settings.distance_cache_dir.mkdir(parents=True, exist_ok=True)
    if yaml_content is not None:
        config_path = tmp_path / "distance_metrics.yaml"
        config_path.write_text(yaml_content)
        settings.distance_metrics_config_path = config_path
    else:
        settings.distance_metrics_config_path = tmp_path / "missing.yaml"
    return settings


# --- MetricConfig validation ------------------------------------------------


def test_metric_config_accepts_defaults() -> None:
    from nyc_apartments_map.processing.distance_metrics import MetricConfig

    m = MetricConfig(name="distance_from_x", destination="1 Main St")
    assert m.mode == "transit"
    assert m.units == "metric"
    assert m.departure_time is None


def test_metric_config_rejects_bad_mode() -> None:
    from pydantic import ValidationError

    from nyc_apartments_map.processing.distance_metrics import MetricConfig

    with pytest.raises(ValidationError):
        MetricConfig(name="x", destination="y", mode="flying")


def test_metric_config_rejects_bad_units() -> None:
    from pydantic import ValidationError

    from nyc_apartments_map.processing.distance_metrics import MetricConfig

    with pytest.raises(ValidationError):
        MetricConfig(name="x", destination="y", units="nautical")


def test_metric_config_rejects_bad_name() -> None:
    from pydantic import ValidationError

    from nyc_apartments_map.processing.distance_metrics import MetricConfig

    with pytest.raises(ValidationError):
        MetricConfig(name="123bad", destination="y")
    with pytest.raises(ValidationError):
        MetricConfig(name="", destination="y")
    with pytest.raises(ValidationError):
        MetricConfig(name="has spaces", destination="y")


def test_metric_config_rejects_bad_departure_time() -> None:
    from pydantic import ValidationError

    from nyc_apartments_map.processing.distance_metrics import MetricConfig

    with pytest.raises(ValidationError):
        MetricConfig(name="x", destination="y", departure_time="invalid")
    with pytest.raises(ValidationError):
        MetricConfig(name="x", destination="y", departure_time="next_funday_09:00")
    with pytest.raises(ValidationError):
        MetricConfig(name="x", destination="y", departure_time="next_monday_99:00")


def test_metric_config_accepts_valid_departure_times() -> None:
    from nyc_apartments_map.processing.distance_metrics import MetricConfig

    m1 = MetricConfig(name="x", destination="y", departure_time="now")
    assert m1.departure_time == "now"
    m2 = MetricConfig(name="x", destination="y", departure_time="next_monday_09:00")
    assert m2.departure_time == "next_monday_09:00"
    m3 = MetricConfig(name="x", destination="y", departure_time=None)
    assert m3.departure_time is None


def test_metric_config_cache_key_stable() -> None:
    from nyc_apartments_map.processing.distance_metrics import MetricConfig

    m = MetricConfig(name="x", destination="y", mode="transit")
    key1 = m.cache_key(["AA0101", "BB0201"])
    key2 = m.cache_key(["AA0101", "BB0201"])
    assert key1 == key2
    key3 = m.cache_key(["BB0201", "AA0101"])
    assert key1 != key3  # order matters


# --- Config loading ---------------------------------------------------------


def test_load_distance_config_parses(tmp_path) -> None:
    from nyc_apartments_map.processing.distance_metrics import load_distance_config

    settings = _settings_with_config(tmp_path, _VALID_YAML)
    configs = load_distance_config(settings)
    assert len(configs) == 1
    assert configs[0].name == "distance_from_times_square"
    assert configs[0].destination == "Times Square, New York, NY"
    assert configs[0].mode == "transit"
    assert configs[0].departure_time == "next_monday_09:00"


def test_load_distance_config_multiple_entries(tmp_path) -> None:
    from nyc_apartments_map.processing.distance_metrics import load_distance_config

    settings = _settings_with_config(tmp_path, _TWO_METRIC_YAML)
    configs = load_distance_config(settings)
    assert len(configs) == 2
    assert configs[0].name == "distance_from_times_square"
    assert configs[1].name == "distance_from_penn_station"
    assert configs[1].mode == "walking"


def test_load_distance_config_missing_file(tmp_path) -> None:
    from nyc_apartments_map.processing.distance_metrics import load_distance_config

    settings = _settings_with_config(tmp_path, None)
    configs = load_distance_config(settings)
    assert configs == []


def test_load_distance_config_empty_metrics(tmp_path) -> None:
    from nyc_apartments_map.processing.distance_metrics import load_distance_config

    settings = _settings_with_config(tmp_path, "metrics: []\n")
    configs = load_distance_config(settings)
    assert configs == []


def test_load_distance_config_skips_invalid_entry(tmp_path) -> None:
    from nyc_apartments_map.processing.distance_metrics import load_distance_config

    yaml_text = """
metrics:
  - name: good_metric
    destination: "Times Square"
  - name: bad_metric
    destination: "x"
    mode: flying
  - name: another_good
    destination: "Penn Station"
"""
    settings = _settings_with_config(tmp_path, yaml_text)
    configs = load_distance_config(settings)
    assert len(configs) == 2
    assert configs[0].name == "good_metric"
    assert configs[1].name == "another_good"


# --- Departure time resolution ---------------------------------------------


def test_resolve_departure_time_none() -> None:
    from nyc_apartments_map.processing.distance_metrics import _resolve_departure_time

    assert _resolve_departure_time(None) is None


def test_resolve_departure_time_now() -> None:
    from nyc_apartments_map.processing.distance_metrics import _resolve_departure_time

    ts = _resolve_departure_time("now")
    assert abs(ts - int(time.time())) < 5


def test_resolve_departure_time_next_monday() -> None:
    from nyc_apartments_map.processing.distance_metrics import _resolve_departure_time

    ts = _resolve_departure_time("next_monday_09:00")
    target = datetime.fromtimestamp(ts)
    assert target.weekday() == 0  # Monday
    assert target.hour == 9
    assert target.minute == 0
    # "next" Monday should be at least 1 day away
    delta = target - datetime.now()
    assert delta.days >= 1


# --- NTA representative points ----------------------------------------------


def test_nta_representative_points_inside_polygon() -> None:
    from nyc_apartments_map.processing.distance_metrics import _nta_representative_points

    boundaries = _two_nta_boundaries()
    points = _nta_representative_points(boundaries)
    assert len(points) == 2
    assert set(points["nta_code"]) == {"AA0101", "BB0201"}
    assert {"nta_code", "latitude", "longitude"} <= set(points.columns)
    for _, row in points.iterrows():
        pt = Point(row["longitude"], row["latitude"])
        nta_poly = boundaries[boundaries["nta_code"] == row["nta_code"]].geometry.iloc[0]
        assert nta_poly.contains(pt)


# --- distance_metrics_nta main function ------------------------------------


def test_distance_metrics_nta_basic(tmp_path, monkeypatch) -> None:
    """Monkeypatched API calls produce the expected columns and values."""
    from nyc_apartments_map.processing.distance_metrics import distance_metrics_nta

    settings = _settings_with_config(tmp_path, _VALID_YAML)
    boundaries = _two_nta_boundaries()

    monkeypatch.setattr(
        "nyc_apartments_map.processing.distance_metrics._geocode_destination",
        lambda address, key, client: (40.7580, -73.9855),
    )
    monkeypatch.setattr(
        "nyc_apartments_map.processing.distance_metrics._distance_matrix_page",
        lambda origins, dest, metric, key, client: [
            {"distance_m": 5000.0, "duration_s": 600.0} for _ in origins
        ],
    )

    out = distance_metrics_nta(settings, boundaries=boundaries)
    assert "nta_code" in out.columns
    assert "distance_from_times_square_m" in out.columns
    assert "distance_from_times_square_s" in out.columns
    assert len(out) == 2
    assert (out["distance_from_times_square_m"] == 5000.0).all()
    assert (out["distance_from_times_square_s"] == 600.0).all()
    assert set(out["nta_code"]) == {"AA0101", "BB0201"}


def test_distance_metrics_nta_two_metrics(tmp_path, monkeypatch) -> None:
    """Multiple YAML entries produce distinct column pairs."""
    from nyc_apartments_map.processing.distance_metrics import distance_metrics_nta

    settings = _settings_with_config(tmp_path, _TWO_METRIC_YAML)
    boundaries = _two_nta_boundaries()

    monkeypatch.setattr(
        "nyc_apartments_map.processing.distance_metrics._geocode_destination",
        lambda address, key, client: (40.7580, -73.9855),
    )

    def mock_dm(origins, dest, metric, key, client):
        if metric.name == "distance_from_times_square":
            return [{"distance_m": 5000.0, "duration_s": 600.0} for _ in origins]
        return [{"distance_m": 3000.0, "duration_s": 400.0} for _ in origins]

    monkeypatch.setattr(
        "nyc_apartments_map.processing.distance_metrics._distance_matrix_page",
        mock_dm,
    )

    out = distance_metrics_nta(settings, boundaries=boundaries)
    assert "distance_from_times_square_m" in out.columns
    assert "distance_from_penn_station_m" in out.columns
    assert (out["distance_from_times_square_m"] == 5000.0).all()
    assert (out["distance_from_penn_station_m"] == 3000.0).all()


def test_distance_metrics_nta_no_key(tmp_path) -> None:
    """Missing GOOGLE_API_KEY -> empty frame, no crash."""
    from nyc_apartments_map.processing.distance_metrics import distance_metrics_nta

    settings = _settings_with_config(tmp_path, _VALID_YAML, with_key=False)
    out = distance_metrics_nta(settings, boundaries=_two_nta_boundaries())
    assert out.empty
    assert "nta_code" in out.columns


def test_distance_metrics_nta_no_config(tmp_path) -> None:
    """Missing config file -> empty frame."""
    from nyc_apartments_map.processing.distance_metrics import distance_metrics_nta

    settings = _settings_with_config(tmp_path, None)
    out = distance_metrics_nta(settings, boundaries=_two_nta_boundaries())
    assert out.empty


def test_distance_metrics_nta_empty_boundaries(tmp_path) -> None:
    """Empty boundaries frame -> empty result."""
    from nyc_apartments_map.processing.distance_metrics import distance_metrics_nta

    settings = _settings_with_config(tmp_path, _VALID_YAML)
    empty_bounds = gpd.GeoDataFrame(
        {"nta_code": [], "geometry": []}, geometry="geometry", crs="EPSG:4326"
    )
    out = distance_metrics_nta(settings, boundaries=empty_bounds)
    assert out.empty


def test_distance_metrics_nta_api_error_leaves_nan(tmp_path, monkeypatch) -> None:
    """httpx errors per-metric produce NaN columns, don't crash the rest."""
    import httpx

    from nyc_apartments_map.processing.distance_metrics import distance_metrics_nta

    settings = _settings_with_config(tmp_path, _TWO_METRIC_YAML)
    boundaries = _two_nta_boundaries()

    call_count = [0]

    def mock_geocode(address, key, client):
        call_count[0] += 1
        if "times_square" in address.lower().replace(" ", "_") or "Times" in address:
            raise httpx.ConnectError("simulated network error")
        return (40.7505, -73.9934)

    monkeypatch.setattr(
        "nyc_apartments_map.processing.distance_metrics._geocode_destination",
        mock_geocode,
    )
    monkeypatch.setattr(
        "nyc_apartments_map.processing.distance_metrics._distance_matrix_page",
        lambda origins, dest, metric, key, client: [
            {"distance_m": 2000.0, "duration_s": 300.0} for _ in origins
        ],
    )

    out = distance_metrics_nta(settings, boundaries=boundaries)
    assert "distance_from_times_square_m" in out.columns
    assert "distance_from_penn_station_m" in out.columns
    assert out["distance_from_times_square_m"].isna().all()
    assert (out["distance_from_penn_station_m"] == 2000.0).all()


# --- Caching ---------------------------------------------------------------


def test_cache_hit_skips_api(tmp_path, monkeypatch) -> None:
    """A fresh cache file means the API is never called on the second run."""
    from nyc_apartments_map.processing.distance_metrics import distance_metrics_nta

    settings = _settings_with_config(tmp_path, _VALID_YAML)
    boundaries = _two_nta_boundaries()

    # First run: populate cache via mocked API.
    monkeypatch.setattr(
        "nyc_apartments_map.processing.distance_metrics._geocode_destination",
        lambda address, key, client: (40.7580, -73.9855),
    )
    monkeypatch.setattr(
        "nyc_apartments_map.processing.distance_metrics._distance_matrix_page",
        lambda origins, dest, metric, key, client: [
            {"distance_m": 5000.0, "duration_s": 600.0} for _ in origins
        ],
    )
    out1 = distance_metrics_nta(settings, boundaries=boundaries)
    assert (out1["distance_from_times_square_m"] == 5000.0).all()

    # Second run: API should NOT be called. Patch to raise if invoked.
    def _fail(*_args, **_kwargs):
        raise AssertionError("API called despite cache hit")

    monkeypatch.setattr(
        "nyc_apartments_map.processing.distance_metrics._geocode_destination", _fail
    )
    monkeypatch.setattr(
        "nyc_apartments_map.processing.distance_metrics._distance_matrix_page", _fail
    )
    out2 = distance_metrics_nta(settings, boundaries=boundaries)
    assert out2.equals(out1)


def test_cache_expired_refetches(tmp_path, monkeypatch) -> None:
    """A cache file older than 30 days triggers a re-fetch."""
    from nyc_apartments_map.processing.distance_metrics import (
        _CACHE_TTL_SECONDS,
        DISTANCE_METRIC_FUNCS,
        MetricConfig,
        distance_metrics_nta,
    )

    assert len(DISTANCE_METRIC_FUNCS) == 1

    settings = _settings_with_config(tmp_path, _VALID_YAML)
    boundaries = _two_nta_boundaries()
    nta_codes = ["AA0101", "BB0201"]

    # Write a stale cache file (>30 days old) with sentinel values.
    metric = MetricConfig(
        name="distance_from_times_square",
        destination="Times Square, New York, NY",
        mode="transit",
        departure_time="next_monday_09:00",
        units="metric",
    )
    cache_path = settings.distance_cache_dir / f"{metric.cache_key(nta_codes)}.json"
    old_ts = datetime.now(UTC) - timedelta(seconds=_CACHE_TTL_SECONDS + 3600)
    cache_path.write_text(
        json.dumps(
            {
                "fetched_at": old_ts.isoformat(),
                "config": metric.model_dump(),
                "nta_codes": nta_codes,
                "results": [{"distance_m": 999.0, "duration_s": 999.0} for _ in nta_codes],
            }
        )
    )

    geocode_called = [False]

    def mock_geocode(address, key, client):
        geocode_called[0] = True
        return (40.7580, -73.9855)

    monkeypatch.setattr(
        "nyc_apartments_map.processing.distance_metrics._geocode_destination",
        mock_geocode,
    )
    monkeypatch.setattr(
        "nyc_apartments_map.processing.distance_metrics._distance_matrix_page",
        lambda origins, dest, metric, key, client: [
            {"distance_m": 5000.0, "duration_s": 600.0} for _ in origins
        ],
    )

    out = distance_metrics_nta(settings, boundaries=boundaries)
    assert geocode_called[0] is True  # API was called (cache expired)
    assert (out["distance_from_times_square_m"] == 5000.0).all()  # new data, not 999.0


def test_cache_corrupt_ignored(tmp_path, monkeypatch) -> None:
    """A corrupt cache file is treated as a miss and re-fetched."""
    from nyc_apartments_map.processing.distance_metrics import (
        MetricConfig,
        distance_metrics_nta,
    )

    settings = _settings_with_config(tmp_path, _VALID_YAML)
    boundaries = _two_nta_boundaries()
    nta_codes = ["AA0101", "BB0201"]

    metric = MetricConfig(
        name="distance_from_times_square",
        destination="Times Square, New York, NY",
    )
    cache_path = settings.distance_cache_dir / f"{metric.cache_key(nta_codes)}.json"
    cache_path.write_text("not valid json {{{")

    geocode_called = [False]

    def mock_geocode(address, key, client):
        geocode_called[0] = True
        return (40.7580, -73.9855)

    monkeypatch.setattr(
        "nyc_apartments_map.processing.distance_metrics._geocode_destination",
        mock_geocode,
    )
    monkeypatch.setattr(
        "nyc_apartments_map.processing.distance_metrics._distance_matrix_page",
        lambda origins, dest, metric, key, client: [
            {"distance_m": 5000.0, "duration_s": 600.0} for _ in origins
        ],
    )

    out = distance_metrics_nta(settings, boundaries=boundaries)
    assert geocode_called[0] is True
    assert (out["distance_from_times_square_m"] == 5000.0).all()


# --- Registry ---------------------------------------------------------------


def test_distance_metric_funcs_list_complete() -> None:
    from nyc_apartments_map.processing.distance_metrics import DISTANCE_METRIC_FUNCS

    names = {fn.__name__ for fn in DISTANCE_METRIC_FUNCS}
    assert names == {"distance_metrics_nta"}
