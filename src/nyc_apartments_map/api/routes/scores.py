"""HTTP routes: re-score NTAs and read the weight profile.

Routes are thin: they pull :class:`Settings` via FastAPI dependency injection
(overridable in tests), call the framework-agnostic service, and map the
service's typed exceptions to HTTP status codes.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status

from nyc_apartments_map.api.schemas import (
    CompositeWeightsRequest,
    ScoreResponse,
    WeightProfileOut,
)
from nyc_apartments_map.api.scoring_service import (
    IndicatorsUnavailable,
    ProfileMismatch,
    build_profile_out,
    compute_scores,
)
from nyc_apartments_map.config import Settings, get_settings

router = APIRouter(prefix="/api", tags=["scores"])


@router.get("/weights", response_model=WeightProfileOut)
def get_weights(
    settings: Annotated[Settings, Depends(get_settings)],
) -> WeightProfileOut:
    """Return the current ``weights.yaml`` to pre-fill the UI's sliders."""
    out = build_profile_out(settings)
    if out is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, f"Weights file not found at {settings.weights_path}"
        )
    return WeightProfileOut.model_validate(out)


@router.post("/scores", response_model=ScoreResponse)
def post_scores(
    request: CompositeWeightsRequest,
    settings: Annotated[Settings, Depends(get_settings)],
) -> ScoreResponse:
    """Re-score every NTA under the posted composite weights.

    Request body: composite sub-score weights (non-negative, need not sum to 1;
    normalized server-side). Response: per-NTA ``desirability_score``,
    sub-scores, and a precomputed hex ``color`` for the restyle, plus the
    colormap ``domain`` for legends/tooltips.
    """
    try:
        data = compute_scores(settings, request.as_composite_dict())
    except ProfileMismatch as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    except IndicatorsUnavailable as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
    return ScoreResponse(**data)
