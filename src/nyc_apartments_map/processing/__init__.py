"""Processing package: cleaning + normalization + NTA enrichment + aggregation."""

from nyc_apartments_map.processing.aggregate import build_nta_indicators
from nyc_apartments_map.processing.enrich import enrich_listings
from nyc_apartments_map.processing.normalize import (
    clean_loader,
    load_normalized,
    normalize,
    validate_schema,
)

__all__ = [
    "build_nta_indicators",
    "clean_loader",
    "enrich_listings",
    "load_normalized",
    "normalize",
    "validate_schema",
]
