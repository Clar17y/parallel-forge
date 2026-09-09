"""Fixture-owned constrained evaluator for the greeting exercise."""

import ast
import json
from pathlib import Path


def _greeting_expression(node: ast.AST, parameter: str) -> bool:
    if isinstance(node, ast.Constant):
        return isinstance(node.value, str)
    if isinstance(node, ast.Name):
        return node.id == parameter
    if isinstance(node, ast.JoinedStr):
        return all(
            isinstance(value, ast.Constant)
            and isinstance(value.value, str)
            or isinstance(value, ast.FormattedValue)
            and isinstance(value.value, ast.Name)
            and value.value.id == parameter
            and value.format_spec is None
            for value in node.values
        )
    return (
        isinstance(node, ast.BinOp)
        and isinstance(node.op, ast.Add)
        and _greeting_expression(node.left, parameter)
        and _greeting_expression(node.right, parameter)
    )


def _string_annotation(node: ast.AST | None) -> bool:
    return node is None or isinstance(node, ast.Name) and node.id == "str"


def _load_greet(path: Path):
    module = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    if len(module.body) != 1 or not isinstance(module.body[0], ast.FunctionDef):
        raise AssertionError("app must contain only greet")
    function = module.body[0]
    if (
        function.name != "greet"
        or function.decorator_list
        or function.args.vararg is not None
        or function.args.kwarg is not None
        or len(function.args.args) != 1
        or function.args.posonlyargs
        or function.args.kwonlyargs
        or any(
            not isinstance(value, ast.Constant) or not isinstance(value.value, str)
            for value in function.args.defaults
        )
        or function.args.kw_defaults
        or not _string_annotation(function.args.args[0].annotation)
        or not _string_annotation(function.returns)
        or getattr(function, "type_params", ())
        or len(function.body) != 1
        or not isinstance(function.body[0], ast.Return)
        or function.body[0].value is None
    ):
        raise AssertionError("greet must be one pure return expression")
    parameter = function.args.args[0].arg
    if not _greeting_expression(function.body[0].value, parameter):
        raise AssertionError("greet expression is outside the fixture grammar")
    safe_function = ast.FunctionDef(
        name="greet",
        args=ast.arguments(
            posonlyargs=[],
            args=[ast.arg(arg=parameter)],
            kwonlyargs=[],
            kw_defaults=[],
            defaults=[ast.Constant(value=value.value) for value in function.args.defaults],
        ),
        body=[ast.Return(value=function.body[0].value)],
        decorator_list=[],
    )
    safe_module = ast.fix_missing_locations(ast.Module(body=[safe_function], type_ignores=[]))
    namespace = {"__builtins__": {}}
    exec(compile(safe_module, str(path), "exec"), namespace)  # noqa: S102 - rebuilt AST only
    return namespace["greet"]


def _valid_test_expression(node: ast.AST) -> bool:
    if isinstance(node, ast.Constant):
        return isinstance(node.value, (str, int, bool, type(None)))
    if isinstance(node, ast.Call):
        return (
            isinstance(node.func, ast.Name)
            and node.func.id == "greet"
            and not node.keywords
            and all(_valid_test_expression(arg) for arg in node.args)
        )
    if isinstance(node, ast.Compare):
        return _valid_test_expression(node.left) and all(
            isinstance(op, (ast.Eq, ast.NotEq)) and _valid_test_expression(value)
            for op, value in zip(node.ops, node.comparators, strict=True)
        )
    return isinstance(node, ast.BoolOp) and all(
        _valid_test_expression(value) for value in node.values
    )


def _run_submitted_tests(path: Path, greet: object) -> None:
    module = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    functions: list[ast.FunctionDef] = []
    for node in module.body:
        if isinstance(node, ast.ImportFrom):
            if (
                node.module != "app"
                or node.level != 0
                or [(item.name, item.asname) for item in node.names] != [("greet", None)]
            ):
                raise AssertionError("tests may import only app.greet")
        elif isinstance(node, ast.FunctionDef):
            if (
                not node.name.startswith("test_")
                or any(function.name == node.name for function in functions)
                or node.decorator_list
                or node.args.args
                or node.args.posonlyargs
                or node.args.kwonlyargs
                or node.args.vararg is not None
                or node.args.kwarg is not None
                or node.args.defaults
                or node.args.kw_defaults
                or (
                    node.returns is not None
                    and not (isinstance(node.returns, ast.Constant) and node.returns.value is None)
                )
                or getattr(node, "type_params", ())
                or not node.body
                or any(
                    not isinstance(s, ast.Assert)
                    or s.msg is not None
                    or not _valid_test_expression(s.test)
                    for s in node.body
                )
            ):
                raise AssertionError("tests must be pure no-argument assertions")
            functions.append(
                ast.FunctionDef(
                    name=node.name,
                    args=ast.arguments(
                        posonlyargs=[], args=[], kwonlyargs=[], kw_defaults=[], defaults=[]
                    ),
                    body=[ast.Assert(test=s.test, msg=None) for s in node.body],
                    decorator_list=[],
                )
            )
        else:
            raise TypeError("tests may contain only app.greet import and test functions")
    if not functions:
        raise AssertionError("at least one submitted test is required")
    namespace = {"__builtins__": {}, "greet": greet}
    safe_module = ast.fix_missing_locations(ast.Module(body=functions, type_ignores=[]))
    exec(compile(safe_module, str(path), "exec"), namespace)  # noqa: S102 - validated AST only
    for function in functions:
        namespace[function.name]()


def main() -> None:
    root = Path(__file__).parent
    greet = _load_greet(root / "app.py")
    assert greet("world") == "Hello, world!"
    _run_submitted_tests(root / "test_app.py", greet)
    print(
        "FORGE_EVAL_REPORT_V1:"
        + json.dumps(
            {
                "report_version": 1,
                "fixture_version": "eval-fixture-v1",
                "case_key": "developer/basic-change",
                "command_name": "pytest",
                "tests": {"test_app.py": True},
                "assertions": {"greet_returns_hello": True},
            }
        )
    )


if __name__ == "__main__":
    main()
