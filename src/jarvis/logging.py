"""Configure logging within the jarvis namespace."""

import logging

from jarvis.config import AppConfig


def configure_logging(config: AppConfig) -> None:
    """Replace Jarvis handlers with console and optional UTF-8 file logging."""
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    try:
        if config.log_dir is not None:
            config.log_dir.mkdir(parents=True, exist_ok=True)
            handlers.append(
                logging.FileHandler(
                    config.log_dir / "jarvis.log", encoding="utf-8"
                )
            )
    except OSError:
        for handler in handlers:
            handler.close()
        raise

    close_logging()
    logger = logging.getLogger("jarvis")
    logger.setLevel(config.log_level)
    logger.propagate = False
    for handler in handlers:
        handler.setFormatter(formatter)
        logger.addHandler(handler)


def close_logging() -> None:
    """Close only handlers owned by Jarvis, leaving other loggers untouched."""
    logger = logging.getLogger("jarvis")
    for handler in logger.handlers[:]:
        logger.removeHandler(handler)
        handler.close()
