"""Project configuration: paths and environment-loaded settings."""

from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# src/nyc_apartments_map/config.py is two directories below the repo root:
#   parents[0]=package dir, parents[1]=src/, parents[2]=<repo root>
_PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    """Settings loaded from environment / .env file."""

    model_config = SettingsConfigDict(
        env_file=str(_PROJECT_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    project_root: Path = _PROJECT_ROOT
    data_dir: Path = _PROJECT_ROOT / "data"
    raw_dir: Path = _PROJECT_ROOT / "data" / "raw"
    interim_dir: Path = _PROJECT_ROOT / "data" / "interim"
    processed_dir: Path = _PROJECT_ROOT / "data" / "processed"
    outputs_dir: Path = _PROJECT_ROOT / "outputs"
    maps_dir: Path = _PROJECT_ROOT / "outputs" / "maps"
    normalized_path: Path = _PROJECT_ROOT / "data" / "processed" / "normalized.parquet"
    # 2020 NTA boundary GeoJSON (DCP "Bytes of the Big Apple"). Enrichment reads
    # this for point-in-polygon assignment; absence is non-fatal (skips + warns).
    nta_boundaries_path: Path = _PROJECT_ROOT / "data" / "raw" / "ntas" / "ntas.json"
    # Listing-derived metrics per NTA (one row per NTA, keyed by nta_code).
    nta_indicators_path: Path = _PROJECT_ROOT / "data" / "processed" / "nta_indicators.parquet"
    # Desirability composite-score weight profile (sub-score + metric weights
    # with explicit per-metric direction). Read by processing/scoring.py;
    # absence is non-fatal (scoring skips + warns, mirroring the boundary file).
    weights_path: Path = _PROJECT_ROOT / "weights.yaml"
    default_map_path: Path = _PROJECT_ROOT / "outputs" / "maps" / "nyc_apartments.html"

    # Google Maps API key. First env-var field in Settings — activates the
    # previously-dormant env_file loader (pydantic-settings reads GOOGLE_API_KEY
    # from .env case-insensitively). None -> distance metrics skip gracefully.
    google_api_key: str | None = None
    # YAML file defining distance-from-address metrics (one section per metric).
    # Each entry produces two columns on nta_indicators.parquet: <name>_m and
    # <name>_s. Absence is non-fatal (skips with a warning).
    distance_metrics_config_path: Path = _PROJECT_ROOT / "configs" / "distance_metrics.yaml"
    # Cache dir for raw Google Distance Matrix + Geocoding JSON responses.
    # Honors Google ToS: route cache is re-used only within 30 days of the
    # fetch timestamp recorded inside each cache file.
    distance_cache_dir: Path = _PROJECT_ROOT / "data" / "raw" / "distance_from_address"

    def ensure_dirs(self) -> None:
        """Create all working directories if they don't yet exist."""
        for path in (
            self.data_dir,
            self.raw_dir,
            self.interim_dir,
            self.processed_dir,
            self.outputs_dir,
            self.maps_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)


def get_settings() -> Settings:
    """Return a fresh Settings instance (cheap; no caching needed)."""
    return Settings()
