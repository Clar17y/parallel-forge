from app import greet


def test_greet() -> None:
    assert greet("world") == "Hello, world!"


if __name__ == "__main__":
    import json

    test_greet()
    print("FORGE_EVAL_REPORT_V1:" + json.dumps({
        "report_version": 1,
        "fixture_version": "eval-fixture-v1",
        "case_key": "developer/basic-change",
        "command_name": "pytest",
        "tests": {"test_app.py": True},
        "assertions": {"greet_returns_hello": True},
    }))
