"""Tests for the safe expression evaluator."""

import pytest

from iocmng.core.safe_eval import safe_eval


class TestSafeEval:
    """Core safe_eval functionality."""

    def test_simple_comparison(self):
        assert safe_eval("x == 1", {"x": 1}) is True
        assert safe_eval("x == 1", {"x": 0}) is False

    def test_boolean_and(self):
        assert safe_eval("a == 0 and b == 1", {"a": 0, "b": 1}) is True
        assert safe_eval("a == 0 and b == 1", {"a": 1, "b": 1}) is False

    def test_boolean_or(self):
        assert safe_eval("a == 0 or b == 0", {"a": 1, "b": 0}) is True
        assert safe_eval("a == 0 or b == 0", {"a": 1, "b": 1}) is False

    def test_not_operator(self):
        assert safe_eval("not x", {"x": 0}) is True
        assert safe_eval("not x", {"x": 1}) is False

    def test_greater_less(self):
        assert safe_eval("x > 5", {"x": 10}) is True
        assert safe_eval("x <= 5", {"x": 5}) is True
        assert safe_eval("x < 5", {"x": 5}) is False

    def test_not_equal(self):
        assert safe_eval("x != 0", {"x": 1}) is True
        assert safe_eval("x != 0", {"x": 0}) is False

    def test_arithmetic(self):
        assert safe_eval("a + b > 10", {"a": 6, "b": 7}) is True
        assert safe_eval("a - b == 3", {"a": 8, "b": 5}) is True

    def test_parentheses(self):
        assert safe_eval("(a == 0 or b == 0) and c == 1", {"a": 1, "b": 0, "c": 1}) is True
        assert safe_eval("(a == 0 or b == 0) and c == 1", {"a": 1, "b": 1, "c": 1}) is False

    def test_ternary(self):
        assert safe_eval("1 if x > 0 else 0", {"x": 5}) == 1
        assert safe_eval("1 if x > 0 else 0", {"x": -1}) == 0

    def test_interlock_condition(self):
        """Real condition from softinterlock."""
        readings = {"chlrfd": 0, "llrf1": 1, "chlsld0": 1}
        assert safe_eval("chlrfd == 0 and llrf1 == 1", readings) is True
        readings["chlrfd"] = 1
        assert safe_eval("chlrfd == 0 and llrf1 == 1", readings) is False


class TestSafeEvalSecurity:
    """Verify that unsafe expressions are rejected."""

    def test_unregistered_function_rejected(self):
        with pytest.raises(ValueError, match="not registered"):
            safe_eval("print('hello')", {})

    def test_method_call_rejected(self):
        with pytest.raises(ValueError, match="Unsafe"):
            safe_eval("x.upper()", {"x": "hello"})

    def test_import_rejected(self):
        with pytest.raises(ValueError, match="Unsafe"):
            safe_eval("__import__('os')", {})

    def test_attribute_access_rejected(self):
        with pytest.raises(ValueError, match="Unsafe"):
            safe_eval("x.__class__", {"x": 1})

    def test_subscript_rejected(self):
        with pytest.raises(ValueError, match="Unsafe"):
            safe_eval("x[0]", {"x": [1, 2, 3]})

    def test_lambda_rejected(self):
        with pytest.raises((ValueError, SyntaxError)):
            safe_eval("(lambda: 1)()", {})

    def test_syntax_error(self):
        with pytest.raises(SyntaxError):
            safe_eval("if True:", {})

    def test_undefined_variable(self):
        with pytest.raises(NameError):
            safe_eval("unknown_var == 1", {})


class TestSafeEvalFunctions:
    """Verify that registered function calls work in safe_eval."""

    def test_registered_function_allowed(self):
        assert safe_eval("abs(x)", {"x": -5}) == 5

    def test_mean_function(self):
        result = safe_eval("mean(buf)", {"buf": [1, 2, 3, 4, 5]})
        assert result == 3.0

    def test_sqrt_in_expression(self):
        result = safe_eval("sqrt(x) > 3", {"x": 16})
        assert result is True

    def test_nested_registered_functions(self):
        result = safe_eval("round(sqrt(x))", {"x": 2})
        assert result == 1

    def test_function_in_condition(self):
        assert safe_eval("mean(buf) > 0.5", {"buf": [0.1, 0.9, 0.8]}) is True

    def test_clamp_function(self):
        assert safe_eval("clamp(x, 0, 10)", {"x": 15}) == 10
        assert safe_eval("clamp(x, 0, 10)", {"x": -5}) == 0
        assert safe_eval("clamp(x, 0, 10)", {"x": 5}) == 5

    def test_extra_functions(self):
        custom = {"double": lambda x: x * 2}
        assert safe_eval("double(x)", {"x": 5}, extra_functions=custom) == 10

    def test_extra_function_unregistered_rejected(self):
        with pytest.raises(ValueError, match="not registered"):
            safe_eval("triple(x)", {"x": 5})
