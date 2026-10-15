import asyncio
import logging
logger = logging.getLogger(__name__)

async def backfill_missing_periods(analyzer, registry=None):
    pending = analyzer.pending_period_bounds()
    if not pending:
        return
    logger.info('period catch-up pending_windows=%s', len(pending))
    for start, end in pending:
        if registry is not None and (('period', start.isoformat()) in registry.in_flight or analyzer.period_is_complete(start)):
            continue
        try:
            await analyzer.period(end=end, source='catchup')
        except asyncio.CancelledError:
            raise
        except Exception as error:
            logger.error('period catch-up failed error=%s', type(error).__name__)
