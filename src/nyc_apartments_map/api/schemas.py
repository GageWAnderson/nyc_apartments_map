"""Pydantic request/response models for the scoring API.

The API re-scores NTAs from a caller-supplied composite weight override; the
per-metric sub-score weights and directions stay fixed from ``weights.yaml``
(see :mod:`nyc_apartments_map.processing.scoring`). These models validate the
wire format; the service converts the request into the existing
:class:`WeightProfile` dataclass so the scoring math stays single-sourced.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator


class CompositeWeightsRequest(BaseModel):
    """Composite sub-score weights posted by the map UI's "Update" button.

    One field per composite sub-score in the shipped ``weights.yaml``
    (affordability / safety / quality / amenity). Values are non-negative and
    need not sum to 1 -- the service normalizes them. At least one must be
    positive (an all-zero request is rejected here). The service re-checks
    that these field names match the sub-score keys in ``weights.yaml`` and
    returns 422 on mismatch (so a stale UI is caught loudly rather than
    silently mis-weighting).

    ``extra="forbid"`` rejects unknown sub-score names -- if the profile grows
    a new sub-score, this model and the UI must be updated together.
    """

    model_config = ConfigDict(extra="forbid")

    affordability: float = Field(default=0.0, ge=0.0)
    safety: float = Field(default=0.0, ge=0.0)
    quality: float = Field(default=0.0, ge=0.0)
    amenity: float = Field(default=0.0, ge=0.0)

    @model_validator(mode="after")
    def _at_least_one_positive(self) -> CompositeWeightsRequest:
        if not any(v > 0 for v in self.as_composite_dict().values()):
            raise ValueError("at least one composite weight must be positive")
        return self

    def as_composite_dict(self) -> dict[str, float]:
        """Return the weights as a sub-score name -> weight mapping."""
        return {
            "affordability": self.affordability,
            "safety": self.safety,
            "quality": self.quality,
            "amenity": self.amenity,
        }


class ScoreDomain(BaseModel):
    """1st/99th percentile domain of the composite score, for legends/tooltips."""

    lo: float
    hi: float


class NtaScore(BaseModel):
    """Scores for one NTA under the requested weights.

    ``color`` is the hex fill color precomputed server-side from the same
    branca ``YlOrRd_09`` colormap the map builder uses, so the client does no
    color math -- it just applies ``fillColor``. No-data NTAs get
    ``#cccccc`` (matching the builder's ``_NAN_STYLE``).

    ``sub_scores`` is keyed by sub-score column name (e.g. ``affordability_score``)
    so the client can refresh the tooltip's per-metric breakdown without the
    schema hardcoding the set.
    """

    desirability_score: float | None
    sub_scores: dict[str, float | None]
    color: str


class ScoreResponse(BaseModel):
    """Result of re-scoring all NTAs under the requested composite weights."""

    metric: str
    domain: ScoreDomain
    scores: dict[str, NtaScore]


class MetricOut(BaseModel):
    """One metric within a sub-score of the weight profile."""

    name: str
    weight: float
    direction: str


class SubScoreOut(BaseModel):
    """One sub-score: its composite weight and constituent metrics."""

    name: str
    composite_weight: float
    metrics: list[MetricOut]


class WeightProfileOut(BaseModel):
    """Echo of the current ``weights.yaml`` for pre-filling the UI."""

    composite: dict[str, float]
    sub_scores: list[SubScoreOut]
