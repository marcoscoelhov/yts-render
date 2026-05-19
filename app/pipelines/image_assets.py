from __future__ import annotations

from pathlib import Path
from typing import Any


class ImageAssetDomain:
    def __init__(self, pipeline: Any) -> None:
        self.pipeline = pipeline

    def generate_primary_asset(self, job_id: str, scene: dict[str, Any], output_path: Path) -> dict[str, Any]:
        return self.pipeline._generate_primary_asset(job_id, scene, output_path)

    def normalize_asset_uri_extension(self, asset: dict[str, Any]) -> dict[str, Any]:
        return self.pipeline._normalize_asset_uri_extension(asset)

    def score_asset(self, scene: dict[str, Any], asset: dict[str, Any]) -> dict[str, Any]:
        return self.pipeline._score_asset(scene, asset)

    def asset_scores_pass(self, scores: dict[str, Any]) -> bool:
        return self.pipeline._asset_scores_pass(scores)

    def image_prompt_variants(self, scene: dict[str, Any], regeneration_round: int = 1) -> list[dict[str, Any]]:
        return self.pipeline._image_prompt_variants(scene, regeneration_round)
