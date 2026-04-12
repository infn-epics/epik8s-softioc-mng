"""Tests for the built-in function library."""

import math

import pytest

from iocmng.core.functions import get_registry, register


class TestRegistry:
    """Registry mechanics."""

    def test_get_registry_returns_dict(self):
        reg = get_registry()
        assert isinstance(reg, dict)
        assert len(reg) > 0

    def test_custom_register(self):
        register("_test_double", lambda x: x * 2)
        reg = get_registry()
        assert reg["_test_double"](5) == 10

    def test_registry_is_snapshot(self):
        """Mutating the returned dict must not affect the registry."""
        reg = get_registry()
        reg["__nope__"] = None
        assert "__nope__" not in get_registry()


class TestMath:
    """Math functions."""

    def test_abs(self):
        assert get_registry()["abs"](-3) == 3

    def test_round(self):
        assert get_registry()["round"](2.7) == 3

    def test_sqrt(self):
        assert get_registry()["sqrt"](9) == 3.0

    def test_log(self):
        assert get_registry()["log"](math.e) == pytest.approx(1.0)

    def test_exp(self):
        assert get_registry()["exp"](0) == 1.0

    def test_pow(self):
        assert get_registry()["pow"](2, 10) == 1024

    def test_floor(self):
        assert get_registry()["floor"](2.9) == 2

    def test_ceil(self):
        assert get_registry()["ceil"](2.1) == 3

    def test_clamp(self):
        clamp = get_registry()["clamp"]
        assert clamp(5, 0, 10) == 5
        assert clamp(-5, 0, 10) == 0
        assert clamp(15, 0, 10) == 10


class TestStatistics:
    """Statistics functions."""

    def test_mean_list(self):
        assert get_registry()["mean"]([1, 2, 3, 4, 5]) == 3.0

    def test_mean_scalar(self):
        assert get_registry()["mean"](7) == 7.0

    def test_mean_empty(self):
        assert get_registry()["mean"]([]) == 0.0

    def test_std(self):
        std = get_registry()["std"]
        result = std([2, 4, 4, 4, 5, 5, 7, 9])
        assert result == pytest.approx(2.0, abs=0.01)

    def test_std_single(self):
        assert get_registry()["std"]([5]) == 0.0

    def test_variance(self):
        var = get_registry()["variance"]
        result = var([2, 4, 4, 4, 5, 5, 7, 9])
        assert result == pytest.approx(4.0, abs=0.01)

    def test_median_odd(self):
        assert get_registry()["median"]([3, 1, 2]) == 2

    def test_median_even(self):
        assert get_registry()["median"]([1, 2, 3, 4]) == 2.5

    def test_rms(self):
        rms = get_registry()["rms"]
        # rms([3, 4]) = sqrt((9+16)/2) = sqrt(12.5) ≈ 3.536
        assert rms([3, 4]) == pytest.approx(math.sqrt(12.5))

    def test_min_list(self):
        assert get_registry()["min"]([5, 1, 3]) == 1

    def test_max_list(self):
        assert get_registry()["max"]([5, 1, 3]) == 5


class TestLogic:
    """Logic functions."""

    def test_any_of_true(self):
        assert get_registry()["any_of"](0, 0, 1) is True

    def test_any_of_false(self):
        assert get_registry()["any_of"](0, 0, 0) is False

    def test_all_of_true(self):
        assert get_registry()["all_of"](1, 1, 1) is True

    def test_all_of_false(self):
        assert get_registry()["all_of"](1, 0, 1) is False

    def test_count_true(self):
        assert get_registry()["count_true"](1, 0, 1, 1, 0) == 3


class TestArray:
    """Array/buffer helper functions."""

    def test_length(self):
        assert get_registry()["length"]([1, 2, 3]) == 3

    def test_length_scalar(self):
        assert get_registry()["length"](5) == 1

    def test_sum_of(self):
        assert get_registry()["sum_of"]([1, 2, 3]) == 6

    def test_diff(self):
        assert get_registry()["diff"]([1, 3, 6, 10]) == [2, 3, 4]

    def test_diff_empty(self):
        assert get_registry()["diff"]([5]) == []

    def test_last(self):
        assert get_registry()["last"]([1, 2, 3, 4, 5], 3) == [3, 4, 5]

    def test_last_default(self):
        assert get_registry()["last"]([1, 2, 3]) == [3]

    def test_moving_avg(self):
        ma = get_registry()["moving_avg"]
        assert ma([1, 2, 3, 4, 5], 3) == pytest.approx(4.0)

    def test_moving_avg_full(self):
        ma = get_registry()["moving_avg"]
        assert ma([1, 2, 3, 4, 5]) == pytest.approx(3.0)

    def test_derivative(self):
        assert get_registry()["derivative"]([0, 1, 4, 9]) == [1, 3, 5]
