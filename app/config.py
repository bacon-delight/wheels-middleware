"""Runtime configuration, sourced from environment with production-safe defaults.

Region strategy (see plan): core resources in ap-south-2 (Hyderabad); SES + Textract
are called cross-region in ap-south-1 (Mumbai) because they are not offered in ap-south-2.
Both regions are in India, so data residency is preserved.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache


@dataclass(frozen=True)
class Settings:
    # --- Regions ---
    core_region: str = field(default_factory=lambda: os.getenv("CORE_REGION", "ap-south-2"))
    ses_region: str = field(default_factory=lambda: os.getenv("SES_REGION", "ap-south-1"))
    textract_region: str = field(
        default_factory=lambda: os.getenv("TEXTRACT_REGION", "ap-south-1")
    )

    # --- LLM ---
    # "fallback" tries Bedrock (in-region) then the Anthropic API; also "bedrock" | "anthropic".
    llm_provider: str = field(default_factory=lambda: os.getenv("LLM_PROVIDER", "fallback"))
    # ap-south-2 Bedrock requires cross-region INFERENCE PROFILE ids (global./apac.), not bare
    # model ids. Interim primary: Sonnet 4.6 is access-granted today and passes the golden eval
    # 20/20. Flip EXTRACT_MODEL to "global.anthropic.claude-sonnet-5" once its Bedrock access
    # request is approved. The Anthropic-API fallback ignores this and uses anthropic_extract_model.
    extract_model: str = field(
        default_factory=lambda: os.getenv("EXTRACT_MODEL", "global.anthropic.claude-sonnet-4-6")
    )
    classify_model: str = field(
        default_factory=lambda: os.getenv(
            "CLASSIFY_MODEL", "global.anthropic.claude-haiku-4-5-20251001-v1:0"
        )
    )
    anthropic_extract_model: str = field(
        default_factory=lambda: os.getenv("ANTHROPIC_EXTRACT_MODEL", "claude-sonnet-5")
    )
    anthropic_api_key_env: str = "ANTHROPIC_API_KEY"

    # --- Storage ---
    table_name: str = field(default_factory=lambda: os.getenv("TABLE_NAME", "wheels"))
    docs_bucket: str = field(default_factory=lambda: os.getenv("DOCS_BUCKET", "wheels-docs-local"))

    # --- Email ---
    from_email: str = field(
        default_factory=lambda: os.getenv("FROM_EMAIL", "no-reply@logiforma.dev")
    )

    # --- Review thresholds ---
    # Fields at or below this confidence are flagged needs_review and sorted first.
    review_confidence_threshold: float = field(
        default_factory=lambda: float(os.getenv("REVIEW_CONFIDENCE_THRESHOLD", "0.80"))
    )
    # Page render DPI for the split-screen left panel.
    render_dpi: int = field(default_factory=lambda: int(os.getenv("RENDER_DPI", "150")))


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
