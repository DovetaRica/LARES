from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from .backup import create_backup

def make_scheduler(settings, analyzer, aggregator, database, segment_memory=None, arrival_tracker=None):
    scheduler = AsyncIOScheduler(timezone=settings.timezone)
    if arrival_tracker is not None:
        scheduler.add_job(arrival_tracker.flush_pending, 'interval', seconds=5, id='arrival_confirmation', max_instances=1, coalesce=True)
    scheduler.add_job(aggregator.flush_environment, 'interval', minutes=settings.env_window_minutes, id='environment_flush', max_instances=1)
    drain = getattr(analyzer, 'drain_question_queue', None) if analyzer is not None else None
    if drain is not None:
        scheduler.add_job(drain, CronTrigger(minute=1, timezone=settings.timezone), id='question_drain', max_instances=1, coalesce=True, misfire_grace_time=1800)
    if segment_memory is not None:
        scheduler.add_job(segment_memory.check_and_compact, 'interval', minutes=settings.segment_flush_interval_minutes, id='segment_flush', max_instances=1)
    scheduler.add_job(analyzer.period, CronTrigger(hour='0,6,12,18', minute=5, timezone=settings.timezone), id='period_analysis', max_instances=1, coalesce=True, misfire_grace_time=1800)
    scheduler.add_job(analyzer.daily, CronTrigger(hour=4, minute=0, timezone=settings.timezone), id='daily_analysis', max_instances=1, coalesce=True, misfire_grace_time=3600)
    scheduler.add_job(analyzer.weekly, CronTrigger(day_of_week='mon', hour=4, minute=30, timezone=settings.timezone), id='weekly_analysis', max_instances=1, coalesce=True, misfire_grace_time=3600)
    scheduler.add_job(create_backup, CronTrigger(hour=3, minute=30, timezone=settings.timezone), args=[database, settings.backup_dir, False], id='daily_backup')
    scheduler.add_job(create_backup, CronTrigger(day_of_week='mon', hour=3, minute=45, timezone=settings.timezone), args=[database, settings.backup_dir, True], id='weekly_backup')
    return scheduler
