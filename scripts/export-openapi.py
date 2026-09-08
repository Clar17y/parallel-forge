"""Write or verify the deterministic dashboard API contract without connecting."""

import argparse
import json
from pathlib import Path

from forge.api.app import create_app
from forge.settings import Settings


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--write", action="store_true", help="Update an existing generated contract"
    )
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    output = root / "apps" / "web" / "openapi.json"
    settings = Settings(
        _env_file=None,
        process_role="api",
        database_url="postgresql+asyncpg://forge:forge@127.0.0.1:5435/forge",
        data_root=root / ".llm-output" / "openapi",
        web_origin="http://127.0.0.1:3000",
        provider_secret_reference="",
        google_api_key_reference="",
        runner_image="",
    )
    rendered = json.dumps(create_app(settings).openapi(), indent=2, sort_keys=True) + "\n"
    if output.exists() and output.read_text(encoding="utf-8") != rendered and not args.write:
        print("OpenAPI contract differs; regenerate with --write.")
        return 1
    if not output.exists() or args.write:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered, encoding="utf-8", newline="\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
