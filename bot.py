#!/usr/bin/env python3
"""OpusFun — entry point.

Boots configuration, logging and the Pyrofork client, then blocks until a
termination signal arrives.  All failure modes exit with a clear message and a
non-zero status so process managers (systemd, Docker, PM2) can restart the bot.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

# Make the project importable no matter where the process was launched from.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from config import ConfigError, get_config  # noqa: E402
from core.logger import setup_logging  # noqa: E402

MIN_PYTHON = (3, 11)


async def _run() -> int:
    try:
        config = get_config()
    except ConfigError as exc:
        # Logging is not up yet — write directly to stderr.
        print(f"[config] {exc}", file=sys.stderr)
        return 2

    log = setup_logging(config.log_level, config.log_file)
    log.info("=" * 68)
    log.info("  %s — starting up", config.bot_name)
    log.info("  Owners configured: %d", len(config.owner_ids))
    log.info("  Couple cooldown: %dh", config.couple_cooldown_hours)
    log.info("=" * 68)

    # Imported after logging is configured so startup logs are captured.
    from core.bot import OpusFun
    from core.database import DatabaseError

    bot = OpusFun(config)

    try:
        await bot.start()
    except DatabaseError as exc:
        log.critical("Database unavailable: %s", exc)
        return 3
    except Exception:
        log.critical("Fatal error during startup", exc_info=True)
        return 1

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    try:
        import signal

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, stop_event.set)
            except (NotImplementedError, RuntimeError):
                # Windows / restricted environments fall back to KeyboardInterrupt.
                pass
    except ImportError:  # pragma: no cover
        pass

    try:
        await stop_event.wait()
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        log.info("Shutdown signal received")
        await bot.stop()

    return 0


def main() -> None:
    if sys.version_info < MIN_PYTHON:
        print(
            f"OpusFun requires Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]}+ "
            f"(running {sys.version.split()[0]})",
            file=sys.stderr,
        )
        raise SystemExit(1)

    try:
        raise SystemExit(asyncio.run(_run()))
    except KeyboardInterrupt:
        logging.getLogger("opusfun").info("Interrupted by user")
        raise SystemExit(0)


if __name__ == "__main__":
    main()
