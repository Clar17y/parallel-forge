from pathlib import Path

import yaml


def test_postgres_port_is_bound_to_loopback_only() -> None:
    compose_path = Path(__file__).resolve().parents[3] / "docker-compose.yml"
    compose = yaml.safe_load(compose_path.read_text(encoding="utf-8"))

    assert compose["services"]["postgres"]["ports"] == ["127.0.0.1:5435:5432"]
