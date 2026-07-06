import pytest
from ctx_engine.intelligence.confidence import decay

def test_decay_precision():
    """Verify that decay multiplies correctly and floors at 0.0."""
    assert decay(1.0) == 0.85
    assert decay(0.85) == pytest.approx(0.7225)
    assert decay(0.0) == 0.0
    
    # Verify repeated decays floor at 0.0
    val = 1.0
    for _ in range(100):
        val = decay(val)
    assert val >= 0.0
    assert val < 1e-5

def test_decay_trajectory():
    """Verify the decay path from 1.0 down to ~0.614 after three steps."""
    val = 1.0
    val = decay(val)
    assert val == pytest.approx(0.85)
    val = decay(val)
    assert val == pytest.approx(0.7225)
    val = decay(val)
    assert val == pytest.approx(0.614125)
