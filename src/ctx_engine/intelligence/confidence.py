# Confidence decay logic for the ctx index database.

CONFIDENCE_DECAY_FACTOR: float = 0.85
LOW_CONFIDENCE_THRESHOLD: float = 0.5
LIKELY_STALE_THRESHOLD: float = 0.2

def decay(confidence: float, decay_factor: float = CONFIDENCE_DECAY_FACTOR) -> float:
    """Decay the confidence value by the decay factor, floored at 0.0."""
    return max(0.0, confidence * decay_factor)


def is_low_confidence(confidence: float, low_threshold: float = LOW_CONFIDENCE_THRESHOLD) -> bool:
    """True when confidence has decayed below the low-confidence threshold."""
    return confidence < low_threshold


def is_likely_stale(confidence: float, stale_threshold: float = LIKELY_STALE_THRESHOLD) -> bool:
    """True when confidence has decayed below the likely-stale threshold."""
    return confidence < stale_threshold


def get_thresholds(repo_root=None) -> tuple[float, float]:
    """Return (low_confidence_threshold, likely_stale_threshold) from config.

    Falls back to built-in defaults when no repo or config is available.
    Additive Week 10 helper — existing callers using the module constants
    keep working unchanged.
    """
    if repo_root is None:
        return LOW_CONFIDENCE_THRESHOLD, LIKELY_STALE_THRESHOLD
    try:
        from ctx_engine.config import get_config_value as _get_config_value

        low = float(_get_config_value(repo_root, "confidence.low_confidence_threshold"))
        stale = float(_get_config_value(repo_root, "confidence.likely_stale_threshold"))
        return low, stale
    except Exception:
        return LOW_CONFIDENCE_THRESHOLD, LIKELY_STALE_THRESHOLD
