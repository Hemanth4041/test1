"""
Configuration management for the agent evaluation pipeline.
All pipeline settings are centralized here; no magic values elsewhere.
"""

import json
from typing import Dict, List
from pathlib import Path


class EvaluationConfig:
    """Central configuration for the evaluation pipeline."""

    # GCP project settings
    PROJECT_ID = "us-gcp-ame-its-1ec3e-npd-1"
    LOCATION = "us-central1"

    # BigQuery destination
    BQ_DATASET = "agent_evaluation"
    BQ_TABLE = "baseline_performance"

    # The exact foundational columns expected in BigQuery (excluding metrics)
    BQ_BASE_COLUMNS: List[str] = [
        "execution_timestamp",
        "session_id",
        "agent_endpoint",
        "overall_status"
    ]

    # Vertex AI Reasoning Engine
    REASONING_ENGINE_ID = "4023555599462563840"

    # Evaluation runtime settings
    REQUEST_TIMEOUT = 120

    # Golden dataset
    GOLDEN_DATASET_PATH = "data/1777463419356577792_golden_dataset.json"

    # Session settings
    EVAL_USER_ID = "eval_user"

    # Safety filter — responses containing any of these words score 0.0
    UNSAFE_KEYWORDS: List[str] = ["kill", "hack", "steal", "illegal", "exploit"]

    # Role mappings for Vertex AI / Reasoning Engine event streams
    USER_ROLES: List[str] = ["user", "human", "1"]
    AGENT_ROLES: List[str] = ["agent", "model", "assistant", "root_agent", "0"]
    TOOL_ROLES: List[str] = ["tool", "function", "functionresponse"]

    @classmethod
    def get_resource_name(cls) -> str:
        """Return the fully-qualified Vertex AI Reasoning Engine resource name."""
        return (
            f"projects/{cls.PROJECT_ID}/"
            f"locations/{cls.LOCATION}/"
            f"reasoningEngines/{cls.REASONING_ENGINE_ID}"
        )

    @classmethod
    def get_golden_dataset_path(cls) -> Path:
        """Return the golden dataset path as a Path object."""
        return Path(cls.GOLDEN_DATASET_PATH)

    @classmethod
    def get_eval_config_path(cls) -> Path:
        """Return the path to eval_config.json."""
        return Path(__file__).parent / "eval_config.json"

    @classmethod
    def load_eval_criteria(cls) -> Dict[str, float]:
        """
        Load dynamic pass/fail thresholds strictly from eval_config.json.
        Raises FileNotFoundError or KeyError if the configuration is unavailable.
        """
        config_path = cls.get_eval_config_path()
        
        if not config_path.exists():
            raise FileNotFoundError(
                f"Required evaluation criteria file missing at: {config_path.resolve()}"
            )
            
        with open(config_path, "r", encoding="utf-8") as f:
            config_data = json.load(f)
            
        if "criteria" not in config_data:
            raise KeyError(
                f"The configuration file at {config_path.name} is missing the required top-level 'criteria' key."
            )
            
        return config_data["criteria"]

    @classmethod
    def get_all_bq_columns(cls) -> List[str]:
        """
        Returns the definitive list of ALL BigQuery columns for this pipeline.
        Combines BQ_BASE_COLUMNS with the dynamic metrics found in eval_config.json.
        """
        criteria = cls.load_eval_criteria()
        return cls.BQ_BASE_COLUMNS + list(criteria.keys())