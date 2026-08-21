"""Worker process entry point."""

import logging

from forge.settings import Settings

logger = logging.getLogger(__name__)


def run() -> None:
    """Validate worker settings and leave execution to later task services."""

    settings = Settings(process_role="worker")
    logger.info("Forge worker is ready for role %s", settings.process_role)
