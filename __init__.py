# filename: __init__.py

"""
Evaluation framework for Deloitte Conversational AI Agent.

This package provides tools for:
- Baseline performance evaluation
- Multi-turn conversation testing
- Tool call validation
- Response quality measurement
"""

import os
import google.auth
from dotenv import load_dotenv

load_dotenv()

# Get project configuration
_, project_id = google.auth.default()
os.environ.setdefault("GOOGLE_CLOUD_PROJECT", "us-gcp-ame-its-1ec3e-npd-1")
os.environ.setdefault("GOOGLE_CLOUD_LOCATION", "us-central1")
os.environ.setdefault("GOOGLE_GENAI_USE_VERTEXAI", "True")

from .config import EvaluationConfig
from .run_evaluation_pipeline import run_pipeline

__all__ = ["EvaluationConfig", "run_pipeline"]
