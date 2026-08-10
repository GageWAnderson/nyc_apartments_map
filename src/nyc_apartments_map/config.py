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
    default_map_path: Path = _PROJECT_ROOT / "outputs" / "maps" / "nyc_apartments.html"

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
