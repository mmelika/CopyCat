"""Tests for add_paper_money arithmetic logic."""


def _apply(current: float, amount: float) -> tuple[float, str]:
    """Mirror of the logic inside the add_paper_money callback."""
    if amount <= 0:
        raise ValueError("Amount must be positive")
    new_cash = round(current + amount, 2)
    return new_cash, f"+${amount:,.2f} added. New balance: ${new_cash:,.2f}"


def test_basic_add():
    new_cash, msg = _apply(100.0, 50.0)
    assert new_cash == 150.0
    assert "+$50.00 added" in msg
    assert "$150.00" in msg


def test_fractional():
    new_cash, _ = _apply(100.0, 25.75)
    assert new_cash == 125.75


def test_zero_raises():
    import pytest
    with pytest.raises(ValueError):
        _apply(100.0, 0.0)


def test_negative_raises():
    import pytest
    with pytest.raises(ValueError):
        _apply(100.0, -5.0)


def test_precision():
    new_cash, _ = _apply(99.99, 0.01)
    assert new_cash == 100.0
