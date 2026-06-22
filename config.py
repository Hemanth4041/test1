"""
Configuration management for the agent evaluation pipeline.
All pipeline settings are centralized here; no magic values elsewhere.
"""

import json
from typing import Dict, Any
from pathlib import Path


class EvaluationConfig:
    """Central configuration for the evaluation pipeline."""

    # GCP project settings
    PROJECT_ID = "us-gcp-ame-its-1ec3e-npd-1"
    LOCATION = "us-central1"

    # BigQuery destination
    BQ_DATASET = "agent_evaluation"
    BQ_TABLE = "baseline_performance"

    # Vertex AI Reasoning Engine
    REASONING_ENGINE_ID = "4023555599462563840"

    # Evaluation runtime settings
    REQUEST_TIMEOUT = 120

    @classmethod
    def get_resource_name(cls) -> str:
        """Return the fully-qualified Vertex AI Reasoning Engine resource name."""
        return (
            f"projects/{cls.PROJECT_ID}/"
            f"locations/{cls.LOCATION}/"
            f"reasoningEngines/{cls.REASONING_ENGINE_ID}"
        )

    @classmethod
    def get_eval_config_path(cls) -> Path:
        """Return the path to eval_config.json."""
        return Path(__file__).parent / "eval_config.json"

    @classmethod
    def load_eval_criteria(cls) -> Dict[str, Any]:
        """
        Load pass/fail thresholds from eval_config.json.
        Falls back to hardcoded defaults if the file is missing.
        """
        config_path = cls.get_eval_config_path()
        try:
            with open(config_path, "r") as f:
                return json.load(f).get("criteria", {})
        except FileNotFoundError:
            return {
                "tool_trajectory_avg_score": 0.8,
                "response_match_score": 0.5,
                "groundedness_v1": 0.8,
                "safety_v1": 1.0,
                "multi_turn_task_success_v1": 0.8,
                "multi_turn_trajectory_quality_v1": 0.8,
                "multi_turn_tool_use_quality_v1": 0.8,
                "final_response_match_v2": 0.5,
            }
