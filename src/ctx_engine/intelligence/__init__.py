"""Intelligence layer — confidence decay, taint propagation, and LLM-driven summarization."""

from ctx_engine.intelligence.confidence import decay, CONFIDENCE_DECAY_FACTOR, LOW_CONFIDENCE_THRESHOLD, LIKELY_STALE_THRESHOLD
from ctx_engine.intelligence.taint import propagate_taint
from ctx_engine.intelligence.llm_client import (
    get_anthropic_client,
    get_model_name,
    call_llm_with_retry,
    parse_response,
    batch_files,
    apply_summary_batch,
    SYSTEM_INSTRUCTION,
)

__all__ = [
    "decay",
    "CONFIDENCE_DECAY_FACTOR",
    "LOW_CONFIDENCE_THRESHOLD",
    "LIKELY_STALE_THRESHOLD",
    "propagate_taint",
    "get_anthropic_client",
    "get_model_name",
    "call_llm_with_retry",
    "parse_response",
    "batch_files",
    "apply_summary_batch",
    "SYSTEM_INSTRUCTION",
]
