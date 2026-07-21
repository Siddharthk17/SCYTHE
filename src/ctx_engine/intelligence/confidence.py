# Confidence decay logic for the ctx index database.

CONFIDENCE_DECAY_FACTOR: float = 0.85
LOW_CONFIDENCE_THRESHOLD: float = 0.5
LIKELY_STALE_THRESHOLD: float = 0.2

def decay(confidence: float) -> float:
    """Decay the confidence value by the decay factor, floored at 0.0."""
    return max(0.0, confidence * CONFIDENCE_DECAY_FACTOR)
