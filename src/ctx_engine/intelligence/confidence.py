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
