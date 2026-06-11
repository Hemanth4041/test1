# filename: config.py

"""
Configuration management for evaluation pipeline.
"""

import os
import json
from typing import Dict, Any
from pathlib import Path


class EvaluationConfig:
    """Central configuration for evaluation pipeline."""
    
    # GCP Configuration
    PROJECT_ID = "us-gcp-ame-its-1ec3e-npd-1"
    LOCATION = "us-central1"
    BQ_DATASET = "agent_evaluation_framework"
    BQ_TABLE = "baseline_performance_snapshots"
    
    # Agent Configuration
    REASONING_ENGINE_ID = "4023555599462563840"
    
    # Evaluation Settings
    REQUEST_TIMEOUT = 120  # seconds
    BRANCH_NAME = os.environ.get("BRANCH_NAME", "dev")
    
    @classmethod
    def get_resource_name(cls) -> str:
        """Get the full resource name for the reasoning engine."""
        return (
            f"projects/{cls.PROJECT_ID}/"
            f"locations/{cls.LOCATION}/"
            f"reasoningEngines/{cls.REASONING_ENGINE_ID}"
        )
    
    @classmethod
    def get_eval_config_path(cls) -> Path:
        """Get path to eval_config.json."""
        return Path(__file__).parent / "eval_config.json"
    
    @classmethod
    def get_golden_dataset_path(cls) -> Path:
        """Get path to golden_dataset.json."""
        return Path(__file__).parent / "golden_dataset.json"
    
    @classmethod
    def load_eval_criteria(cls) -> Dict[str, Any]:
        """Load evaluation criteria from config file."""
        config_path = cls.get_eval_config_path()
        try:
            with open(config_path, 'r') as f:
                config = json.load(f)
                return config.get("criteria", {})
        except FileNotFoundError:
            # Fallback thresholds
            return {
                "tool_trajectory_avg_score": 0.9,
                "response_match_score": 0.75,
                "groundedness_v1": 0.8,
                "safety_v1": 1.0,
                "multi_turn_task_success_v1": 0.85,
                "multi_turn_trajectory_quality_v1": 0.75,
                "multi_turn_tool_use_quality_v1": 0.8,
                "final_response_match_v2": 0.75
            }
    
    @classmethod
    def load_golden_dataset(cls) -> Dict[str, Any]:
        """Load golden dataset."""
        dataset_path = cls.get_golden_dataset_path()
        with open(dataset_path, 'r') as f:
            return json.load(f)
