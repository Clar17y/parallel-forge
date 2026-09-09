"""Standalone worker readiness must be observable without verbose provider logs."""

import logging

from forge.worker import main


def test_worker_entrypoint_emits_its_readiness_log(monkeypatch, caplog):
    monkeypatch.setattr(main.logger, "level", logging.WARNING)

    async def worker():
        main.logger.info("Forge worker recovered and is polling")
        logging.getLogger("google.genai").debug("provider debug detail")

    monkeypatch.setattr(main, "run_worker", worker)
    main.run()
    assert "Forge worker recovered and is polling" in caplog.text
    assert "provider debug detail" not in caplog.text
