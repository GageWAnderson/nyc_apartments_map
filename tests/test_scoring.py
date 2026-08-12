"""Tests for processing/scoring.py: composite desirability scoring."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest


def _write_weights(tmp_path, text: str):
    """Write a weights profile to a tmp file and return its path."""
    path = tmp_path / "weights.yaml"
    path.write_text(text)
    return path


# --- Weight profile loading -------------------------------------------------


def test_load_weights_parses_profile(tmp_path) -> None:
    from nyc_apartments_map.config import Settings
    from nyc_apartments_map.processing.scoring import load_weights

    path = _write_weights(
        tmp_path,
        """
composite:
  affordability: 0.5
  amenity: 0.5
affordability:
  median_price: {weight: 0.5, direction: lower}
  median_price_per_bed: {weight: 0.5, direction: lower}
amenity:
  nightlife_per_1k_units: {weight: 1.0, direction: higher}
""",
    )
    settings = Settings(project_root=tmp_path)
    settings.weights_path = path
    profile = load_weights(settings)
    assert profile is not None
    assert profile.composite == {"affordability": 0.5, "amenity": 0.5}
    assert [m.name for m in profile.sub_scores["affordability"]] == [
        "median_price",
        "median_price_per_bed",
    ]
    assert profile.sub_scores["amenity"][0].direction == "higher"


def test_load_weights_missing_file_returns_none(tmp_path) -> None:
    from nyc_apartments_map.config import Settings
    from nyc_apartments_map.processing.scoring import load_weights

    settings = Settings(project_root=tmp_path)
    settings.weights_path = tmp_path / "absent.yaml"
    assert load_weights(settings) is None


def test_parse_profile_invalid_direction_raises() -> None:
    from nyc_apartments_map.processing.scoring import _parse_profile

    raw = {"composite": {"s": 1.0}, "s": {"m": {"weight": 1.0, "direction": "sideways"}}}
    with pytest.raises(ValueError, match="direction"):
        _parse_profile(raw)


def test_parse_profile_missing_composite_raises() -> None:
    from nyc_apartments_map.processing.scoring import _parse_profile

    with pytest.raises(ValueError, match="composite"):
        _parse_profile({"affordability": {}})


def test_parse_profile_metric_missing_field_raises() -> None:
    from nyc_apartments_map.processing.scoring import _parse_profile

    raw = {"composite": {"s": 1.0}, "s": {"m": {"weight": 1.0}}}  # no direction
    with pytest.raises(ValueError, match="direction"):
        _parse_profile(raw)


# --- Rate derivation --------------------------------------------------------


def test_derive_rate_columns_basic() -> None:
    from nyc_apartments_map.processing.scoring import derive_rate_columns

    df = pd.DataFrame(
        {
            "nypd_felony_count": [10, 20, 30, 40],
            "pluto_res_units": [1000, 500, 0, np.nan],
        }
    )
    derive_rate_columns(df)
    assert "felony_per_1k_units" in df.columns
    assert df["felony_per_1k_units"].iloc[0] == pytest.approx(10.0)  # 10/1000*1000
    assert df["felony_per_1k_units"].iloc[1] == pytest.approx(40.0)  # 20/500*1000
    assert pd.isna(df["felony_per_1k_units"].iloc[2])  # zero denominator -> NaN
    assert pd.isna(df["felony_per_1k_units"].iloc[3])  # NaN denominator -> NaN


def test_derive_rate_columns_missing_column_skips() -> None:
    from nyc_apartments_map.processing.scoring import derive_rate_columns

    df = pd.DataFrame({"pluto_res_units": [1000]})
    derive_rate_columns(df)
    # numerator absent -> rate column not created
    assert "felony_per_1k_units" not in df.columns


# --- Ranking ----------------------------------------------------------------


def test_percentile_rank_lower_flips() -> None:
    from nyc_apartments_map.processing.scoring import _percentile_rank

    s = pd.Series([10.0, 20.0, 30.0, 40.0])
    ranked = _percentile_rank(s, "lower")
    # raw rank(pct=True) = [0.25, 0.5, 0.75, 1.0]; flipped -> [0.75, 0.5, 0.25, 0.0]
    assert ranked.tolist() == pytest.approx([0.75, 0.5, 0.25, 0.0])


def test_percentile_rank_higher_keeps() -> None:
    from nyc_apartments_map.processing.scoring import _percentile_rank

    s = pd.Series([10.0, 20.0, 30.0, 40.0])
    ranked = _percentile_rank(s, "higher")
    assert ranked.tolist() == pytest.approx([0.25, 0.5, 0.75, 1.0])


def test_percentile_rank_nan_stays_nan() -> None:
    from nyc_apartments_map.processing.scoring import _percentile_rank

    s = pd.Series([10.0, np.nan, 30.0])
    ranked = _percentile_rank(s, "higher")
    # rank(pct=True) divides by the count of non-NaN values (2 here), so the
    # two valid values get 1/2 and 2/2; NaN keeps NaN.
    assert ranked.iloc[0] == pytest.approx(0.5)
    assert pd.isna(ranked.iloc[1])
    assert ranked.iloc[2] == pytest.approx(1.0)


# --- Sub-score and composite ------------------------------------------------


def test_sub_score_renormalizes_over_available(tmp_path) -> None:
    from nyc_apartments_map.processing.scoring import MetricSpec, _sub_score

    # Two metrics; NTA1 has both, NTA2 has only m_a (m_b NaN).
    df = pd.DataFrame(
        {
            "m_a": [1.0, 3.0],
            "m_b": [2.0, np.nan],
        }
    )
    mask = pd.Series([True, True])
    metrics = [MetricSpec("m_a", 0.5, "higher"), MetricSpec("m_b", 0.5, "higher")]
    out = _sub_score(df, metrics, mask)
    # ranks: m_a=[0.5, 1.0], m_b=[1.0, NaN]
    # NTA1=(0.5*0.5 + 1.0*0.5)/1.0 = 0.75
    # NTA2=(1.0*0.5)/0.5 = 1.0 (renormalized over the single available metric)
    assert out.iloc[0] == pytest.approx(0.75)
    assert out.iloc[1] == pytest.approx(1.0)


def test_sub_score_all_nan_returns_nan() -> None:
    from nyc_apartments_map.processing.scoring import MetricSpec, _sub_score

    df = pd.DataFrame({"m_a": [np.nan, np.nan]})
    mask = pd.Series([True, True])
    metrics = [MetricSpec("m_a", 1.0, "higher")]
    out = _sub_score(df, metrics, mask)
    assert out.isna().all()


def test_sub_score_respects_direction() -> None:
    from nyc_apartments_map.processing.scoring import MetricSpec, _sub_score

    # Higher raw -> lower desirability for "lower" direction.
    df = pd.DataFrame({"m_a": [1.0, 2.0, 3.0]})
    mask = pd.Series([True, True, True])
    out = _sub_score(df, [MetricSpec("m_a", 1.0, "lower")], mask)
    assert out.tolist() == pytest.approx([1.0 - 1 / 3, 1.0 - 2 / 3, 1.0 - 3 / 3])


def test_composite_imputes_missing_neutral() -> None:
    from nyc_apartments_map.processing.scoring import _compose

    df = pd.DataFrame({"a_score": [0.8, np.nan], "b_score": [0.6, 0.6]})
    available = pd.DataFrame({"a": [True, False], "b": [True, True]}, index=df.index)
    out = _compose(df, {"a": 0.5, "b": 0.5}, available)
    # NTA0: 0.5*0.8 + 0.5*0.6 = 0.7
    # NTA1: 0.5*0.5(neutral) + 0.5*0.6 = 0.55
    assert out.iloc[0] == pytest.approx(0.7)
    assert out.iloc[1] == pytest.approx(0.55)


def test_composite_all_missing_returns_nan() -> None:
    from nyc_apartments_map.processing.scoring import _compose

    df = pd.DataFrame({"a_score": [np.nan], "b_score": [np.nan]})
    available = pd.DataFrame({"a": [False], "b": [False]}, index=df.index)
    out = _compose(df, {"a": 0.5, "b": 0.5}, available)
    assert pd.isna(out.iloc[0])


# --- End-to-end add_desirability_scores -------------------------------------


def _score_settings(tmp_path, weights_text: str):
    from nyc_apartments_map.config import Settings

    settings = Settings(project_root=tmp_path)
    settings.weights_path = _write_weights(tmp_path, weights_text)
    return settings


def test_add_desirability_scores_end_to_end(tmp_path) -> None:
    from nyc_apartments_map.processing.scoring import add_desirability_scores

    settings = _score_settings(
        tmp_path,
        """
composite:
  affordability: 0.7
  amenity: 0.3
affordability:
  median_price: {weight: 1.0, direction: lower}
amenity:
  nightlife_per_1k_units: {weight: 1.0, direction: higher}
""",
    )
    # 4 residential NTAs + 1 park. pluto_res_units=1000 so rate == count.
    agg = pd.DataFrame(
        {
            "nta_code": ["N1", "N2", "N3", "N4", "PARK"],
            "nta_type": ["0", "0", "0", "0", "9"],
            "median_price": [2000.0, 3000.0, 4000.0, 5000.0, np.nan],
            "nightlife_venue_count": [5, 10, 15, 20, 0],
            "pluto_res_units": [1000, 1000, 1000, 1000, 0],
        }
    )

    out = add_desirability_scores(agg, settings)
    # Input unchanged.
    assert "desirability_score" not in agg.columns
    assert "desirability_score" in out.columns
    assert "affordability_score" in out.columns
    assert "amenity_score" in out.columns
    assert "felony_per_1k_units" not in out.columns  # felony source not in agg

    residential = out[out["nta_type"] == "0"].reset_index(drop=True)
    # affordability (lower better): price ranks [0.25,0.5,0.75,1.0] -> [0.75,0.5,0.25,0.0]
    assert residential["affordability_score"].tolist() == pytest.approx([0.75, 0.5, 0.25, 0.0])
    # amenity (higher better): nightlife ranks [0.25,0.5,0.75,1.0]
    assert residential["amenity_score"].tolist() == pytest.approx([0.25, 0.5, 0.75, 1.0])
    # composite = 0.7*aff + 0.3*amen, scaled x100
    aff = np.array([0.75, 0.5, 0.25, 0.0])
    amen = np.array([0.25, 0.5, 0.75, 1.0])
    expected = (0.7 * aff + 0.3 * amen) * 100
    assert residential["desirability_score"].tolist() == pytest.approx(expected.tolist())

    # Park: all scores NaN (non-residential).
    park = out[out["nta_code"] == "PARK"].iloc[0]
    assert pd.isna(park["desirability_score"])
    assert pd.isna(park["affordability_score"])


def test_add_desirability_scores_missing_weights_skips(tmp_path) -> None:
    from nyc_apartments_map.config import Settings
    from nyc_apartments_map.processing.scoring import add_desirability_scores

    settings = Settings(project_root=tmp_path)
    settings.weights_path = tmp_path / "absent.yaml"
    agg = pd.DataFrame({"nta_code": ["N1"], "nta_type": ["0"], "median_price": [1000.0]})
    out = add_desirability_scores(agg, settings)
    assert "desirability_score" not in out.columns
    assert out is agg or out.equals(agg)


def test_add_desirability_scores_no_nta_type_skips(tmp_path) -> None:
    from nyc_apartments_map.processing.scoring import add_desirability_scores

    settings = _score_settings(
        tmp_path,
        """
composite:
  affordability: 1.0
affordability:
  median_price: {weight: 1.0, direction: lower}
""",
    )
    agg = pd.DataFrame({"nta_code": ["N1"], "median_price": [1000.0]})
    out = add_desirability_scores(agg, settings)
    assert "desirability_score" not in out.columns


def test_add_desirability_scores_missing_metric_imputed_neutral(tmp_path) -> None:
    from nyc_apartments_map.processing.scoring import add_desirability_scores

    settings = _score_settings(
        tmp_path,
        """
composite:
  affordability: 0.5
  amenity: 0.5
affordability:
  median_price: {weight: 1.0, direction: lower}
amenity:
  nightlife_per_1k_units: {weight: 1.0, direction: higher}
""",
    )
    # 2 NTAs; pluto_res_units=0 -> nightlife rate NaN -> amenity sub-score NaN
    # -> imputed 0.5 neutral in composite.
    agg = pd.DataFrame(
        {
            "nta_code": ["N1", "N2"],
            "nta_type": ["0", "0"],
            "median_price": [2000.0, 4000.0],
            "nightlife_venue_count": [5, 10],
            "pluto_res_units": [0, 0],
        }
    )
    out = add_desirability_scores(agg, settings)
    res = out.set_index("nta_code")
    # affordability: price [2000, 4000] -> ranks [0.5, 1.0] -> flipped [0.5, 0.0]
    assert res.loc["N1", "affordability_score"] == pytest.approx(0.5)
    assert res.loc["N2", "affordability_score"] == pytest.approx(0.0)
    # amenity: no data -> NaN
    assert pd.isna(res.loc["N1", "amenity_score"])
    # composite: 0.5*aff + 0.5*0.5(neutral), scaled x100
    assert res.loc["N1", "desirability_score"] == pytest.approx((0.5 * 0.5 + 0.5 * 0.5) * 100)
    assert res.loc["N2", "desirability_score"] == pytest.approx((0.5 * 0.0 + 0.5 * 0.5) * 100)


def test_add_desirability_scores_composite_scaled_0_100(tmp_path) -> None:
    from nyc_apartments_map.processing.scoring import add_desirability_scores

    settings = _score_settings(
        tmp_path,
        """
composite:
  affordability: 1.0
affordability:
  median_price: {weight: 1.0, direction: lower}
""",
    )
    agg = pd.DataFrame(
        {
            "nta_code": ["N1", "N2"],
            "nta_type": ["0", "0"],
            "median_price": [1000.0, 2000.0],
        }
    )
    out = add_desirability_scores(agg, settings)
    # sub-score stays [0,1]; composite scaled x100.
    assert out["affordability_score"].max() <= 1.0
    assert out["desirability_score"].max() <= 100.0
    assert out["desirability_score"].min() >= 0.0
