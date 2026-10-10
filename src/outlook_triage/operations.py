from __future__ import annotations

import logging

from .config import Settings
from .database import Database
from .filters import load_rules
from .locking import process_lock
from .outlook import OutlookClient
from .service import retry_failed, synchronize
from .soclaas import SoCLaaSClient


def run_sync(db: Database, settings: Settings, logger: logging.Logger) -> dict[str, int]:
    ai = SoCLaaSClient(settings)
    rules = load_rules(settings.rules_file)
    with process_lock(settings.lock_dir):
        with OutlookClient(settings.outlook_profile, timezone=settings.timezone) as outlook:
            return synchronize(
                db=db, outlook=outlook, ai=ai, rules=rules, settings=settings, logger=logger
            )


def run_retry(
    db: Database, settings: Settings, logger: logging.Logger, *, limit: int = 50
) -> dict[str, int]:
    ai = SoCLaaSClient(settings)
    rules = load_rules(settings.rules_file)
    with process_lock(settings.lock_dir):
        with OutlookClient(settings.outlook_profile, timezone=settings.timezone) as outlook:
            return retry_failed(
                db=db,
                outlook=outlook,
                ai=ai,
                rules=rules,
                settings=settings,
                logger=logger,
                limit=limit,
            )
