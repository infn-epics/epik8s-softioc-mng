"""Safe expression evaluator for declarative rule conditions.

Only allows comparisons, boolean logic, arithmetic, and variable/literal
references.  No function calls, attribute access, subscripts, or imports.

Usage::

    from iocmng.core.safe_eval import safe_eval

    result = safe_eval("chlrfd == 0 and llrf1 == 1", {"chlrfd": 0, "llrf1": 1})
    # result == True
"""

from __future__ import annotations

import ast
from typing import Any, Dict

# AST node types that are allowed in rule condition expressions.
_SAFE_NODES = frozenset(
    {
        ast.Expression,
        # Boolean operators
        ast.BoolOp,
        ast.And,
        ast.Or,
        # Unary operators
        ast.UnaryOp,
        ast.Not,
        ast.USub,
        ast.UAdd,
        # Comparisons
        ast.Compare,
        ast.Eq,
        ast.NotEq,
        ast.Lt,
        ast.LtE,
        ast.Gt,
        ast.GtE,
        # Binary arithmetic (for expressions like "a + b > 10")
        ast.BinOp,
        ast.Add,
        ast.Sub,
        ast.Mult,
        ast.Div,
        ast.Mod,
        # Literals and names
        ast.Constant,
        ast.Name,
        ast.Load,
        # Needed for ternary / inline-if (value if cond else other)
        ast.IfExp,
    }
)


def safe_eval(expression: str, variables: Dict[str, Any]) -> Any:
    """Evaluate *expression* with only *variables* in scope.

    Raises :class:`ValueError` on unsafe expressions (function calls,
    attribute access, imports, etc.) and :class:`SyntaxError` on parse
    failure.
    """
    tree = ast.parse(expression, mode="eval")
    for node in ast.walk(tree):
        if type(node) not in _SAFE_NODES:
            raise ValueError(
                f"Unsafe expression — disallowed node: {type(node).__name__}"
            )
    code = compile(tree, "<rule>", "eval")
    return eval(code, {"__builtins__": {}}, variables)
