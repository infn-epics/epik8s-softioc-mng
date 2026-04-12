"""Safe expression evaluator for declarative rule conditions and transforms.

Allows comparisons, boolean logic, arithmetic, variable/literal references,
and **whitelisted function calls** from the built-in function registry
(:mod:`iocmng.core.functions`).

No attribute access, subscripts, imports, or arbitrary callables.

Usage::

    from iocmng.core.safe_eval import safe_eval

    result = safe_eval("chlrfd == 0 and llrf1 == 1", {"chlrfd": 0, "llrf1": 1})
    # result == True

    result = safe_eval("mean(signal_buf) > 0.5", {"signal_buf": [0.1, 0.9, 0.3]})
    # result == True
"""

from __future__ import annotations

import ast
from typing import Any, Dict, Optional, Set

from iocmng.core.functions import get_registry

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
        # Function calls — only for whitelisted names (validated below)
        ast.Call,
        ast.keyword,
        # Tuple/List literals — needed for function arguments like last(buf, 5)
        ast.Tuple,
        ast.List,
    }
)


def _validate_tree(tree: ast.AST, allowed_fns: Set[str]) -> None:
    """Walk the AST and reject any unsafe node or disallowed function call."""
    for node in ast.walk(tree):
        node_type = type(node)
        if node_type is ast.Call:
            # Only allow direct calls to registered function names
            func = node.func
            if not isinstance(func, ast.Name):
                raise ValueError(
                    "Unsafe expression — only direct function calls are allowed"
                )
            if func.id not in allowed_fns:
                raise ValueError(
                    f"Unsafe expression — function {func.id!r} is not registered"
                )
        elif node_type not in _SAFE_NODES:
            raise ValueError(
                f"Unsafe expression — disallowed node: {node_type.__name__}"
            )


def safe_eval(
    expression: str,
    variables: Dict[str, Any],
    extra_functions: Optional[Dict[str, Any]] = None,
) -> Any:
    """Evaluate *expression* with *variables* and registered functions in scope.

    Args:
        expression: A single Python expression string.
        variables: Name → value mapping (inputs, buffers, parameters).
        extra_functions: Additional callables merged into the scope
            (overrides registry entries of the same name).

    Raises:
        ValueError: on unsafe expressions (disallowed AST nodes,
            unregistered function calls).
        SyntaxError: on parse failure.
    """
    tree = ast.parse(expression, mode="eval")

    # Build function scope
    fn_scope = get_registry()
    if extra_functions:
        fn_scope.update(extra_functions)

    _validate_tree(tree, set(fn_scope.keys()))

    code = compile(tree, "<rule>", "eval")
    scope = dict(fn_scope)
    scope.update(variables)
    return eval(code, {"__builtins__": {}}, scope)
