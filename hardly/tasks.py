# Copyright Contributors to the Packit project.
# SPDX-License-Identifier: MIT

import logging
from os import getenv
from socket import gaierror
from typing import List, Optional

from celery import Task
from celery.signals import after_setup_logger
from syslog_rfc5424_formatter import RFC5424Formatter

from hardly.handlers.abstract import TaskName
from hardly.handlers.distgit import (
    DistGitPRHandler,
    SyncFromGitlabMRHandler,
    SyncFromPagurePRHandler,
)
from hardly.jobs import StreamJobs
from packit_service.celerizer import celery_app
from packit_service.constants import (
    DEFAULT_RETRY_LIMIT,
    DEFAULT_RETRY_BACKOFF,
    CELERY_DEFAULT_MAIN_TASK_NAME,
)
from packit_service.utils import load_job_config, load_package_config
from packit_service.worker.result import TaskResults

# Let a remote debugger (Visual Studio Code client)
# access this running instance.
if getenv("DEBUGPY"):
    import debugpy

    # Allow other computers to attach to debugpy at this IP address and port.
    debugpy.listen(("0.0.0.0", 5678))

    # To pause the program until a remote debugger is attached
    print("Waiting for debugger attach")
    debugpy.wait_for_client()
    debugpy.breakpoint()

logger = logging.getLogger(__name__)


# Don't import this (or anything) from p_s.worker.tasks,
# it would create the task from their process_message()
@after_setup_logger.connect
def setup_loggers(logger, *args, **kwargs):
    # debug logs of these are super-duper verbose
    logging.getLogger("requests").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("github").setLevel(logging.WARNING)
    logging.getLogger("botocore").setLevel(logging.WARNING)
    logging.getLogger("s3transfer").setLevel(logging.WARNING)
    # info is just enough
    logging.getLogger("ogr").setLevel(logging.INFO)
    # easier debugging
    logging.getLogger("packit").setLevel(logging.DEBUG)

    syslog_host = getenv("SYSLOG_HOST", "fluentd")
    syslog_port = int(getenv("SYSLOG_PORT", 5140))
    logger.info(f"Setup logging to syslog -> {syslog_host}:{syslog_port}")
    try:
        handler = logging.handlers.SysLogHandler(address=(syslog_host, syslog_port))
    except (ConnectionRefusedError, gaierror):
        logger.info(f"{syslog_host}:{syslog_port} not available")
    else:
        handler.setLevel(logging.DEBUG)
        project = getenv("PROJECT", "hardly")
        handler.setFormatter(RFC5424Formatter(msgid=project))
        logger.addHandler(handler)


# Don't import this (or anything) from p_s.worker.tasks,
# it would create the task from their process_message()
class HandlerTaskWithRetry(Task):
    autoretry_for = (Exception,)
    retry_kwargs = {
        "max_retries": int(getenv("CELERY_RETRY_LIMIT", DEFAULT_RETRY_LIMIT))
    }
    retry_backoff = int(getenv("CELERY_RETRY_BACKOFF", DEFAULT_RETRY_BACKOFF))


@celery_app.task(
    name=getenv("CELERY_MAIN_TASK_NAME") or CELERY_DEFAULT_MAIN_TASK_NAME, bind=True
)
def hardly_process(
    self, event: dict, topic: Optional[str] = None, source: Optional[str] = None
) -> List[TaskResults]:
    """
    Main celery task for processing messages.

    :param event: event data
    :param topic: event topic
    :param source: event source
    :return: dictionary containing task results
    """
    return StreamJobs().process_message(event=event, topic=topic, source=source)


@celery_app.task(name=TaskName.dist_git_pr, base=HandlerTaskWithRetry)
def run_dist_git_sync_handler(event: dict, package_config: dict, job_config: dict):
    handler = DistGitPRHandler(
        package_config=load_package_config(package_config),
        job_config=load_job_config(job_config),
        event=event,
    )
    return get_handlers_task_results(handler.run_job(), event)


@celery_app.task(name=TaskName.sync_from_gitlab_mr, base=HandlerTaskWithRetry)
def run_sync_from_gitlab_mr_handler(
    event: dict, package_config: dict, job_config: dict
):
    handler = SyncFromGitlabMRHandler(
        package_config=load_package_config(package_config),
        job_config=load_job_config(job_config),
        event=event,
    )
    return get_handlers_task_results(handler.run_job(), event)


@celery_app.task(name=TaskName.sync_from_pagure_pr, base=HandlerTaskWithRetry)
def run_sync_from_pagure_pr_handler(
    event: dict, package_config: dict, job_config: dict
):
    handler = SyncFromPagurePRHandler(
        package_config=load_package_config(package_config),
        job_config=load_job_config(job_config),
        event=event,
    )
    return get_handlers_task_results(handler.run_job(), event)


def get_handlers_task_results(results: dict, event: dict) -> dict:
    # include original event to provide more info
    return {"job": results, "event": event}
