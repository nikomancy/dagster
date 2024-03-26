import gc
import logging
import os
import os.path
import threading
from sys import platform
from time import sleep
from typing import Dict, Tuple, Optional, List, TypedDict

import dagster._check as check
from dagster._core.instance import DagsterInstance, InstanceRef
from dagster._core.storage.dagster_run import DagsterRun
from dagster._utils.container import retrieve_containerized_utilization_metrics

RUN_METRICS_POLL_INTERVAL_SECONDS = 30


def _get_platform_name() -> str:
    platform_name = platform
    return platform_name.casefold()


def _process_is_containerized() -> bool:
    """Detect if the current process is running in a container under linux."""
    if _get_platform_name() != "linux":
        return False

    # the root process (pid==1) under linux is expected to be 'init'
    # this test should be simpler and more robust than testing for cgroup v1, v2 or docker
    file = "/proc/1/exe"
    if os.path.isfile(file) and os.access(file, os.R_OK):
        target = os.readlink(file)
        return os.path.split(target)[-1] != "init"

    # /proc/1/exe should exist on linux; if it doesn't, we don't know what kind of system we're on
    return False


def _metric_tags(dagster_run: DagsterRun) -> Dict[str, str]:
    # TODO - organization name, deployment name, etc.
    org_name = "mlarose"
    deployment_name = "test"
    location_name = "test"

    # unique server name ??
    hostname = os.getenv("HOSTNAME", "unknown")

    return {
        "deployment_name": deployment_name,
        "hostname": hostname,
        "job_name": dagster_run.job_name,
        "location_name": location_name,
        "org_name": org_name,
        # "job_origin_id": dagster_run.job_origin_id,
        # "repository_name": dagster_run.repository_name,
        "run_id": dagster_run.run_id
    }


def _report_container_metrics(tags: Dict[str, str], logger: Optional[logging.Logger] = None):
    container_metrics = retrieve_containerized_utilization_metrics(logger)

    if logger:
        logger.debug(f"Container metrics: {container_metrics} to {tags}")

    # # push to graphql
    # res = instance.organization_scoped_graphql_client().execute(
    #     ADD_AGENT_HEARTBEATS_MUTATION,
    #     variable_values={
    #         "serializedAgentHeartbeats": serialized_agent_heartbeats,
    #     },
    #     idempotent_mutation=True,
    # )

    # push to dagster event
    # instance.report_engine_event ?
    pass


def _get_python_runtime_metrics() -> Dict[str, int]:
    return {
        "gc_stats": gc.get_stats(),
        "gc_freeze_count": gc.get_freeze_count()
    }


def _report_python_runtime_metrics(tags: Dict[str, str], logger: Optional[logging.Logger] = None):
    python_runtime_metrics = _get_python_runtime_metrics()

    if logger:
        logger.debug(f"Python runtime metrics: {python_runtime_metrics} to {tags}")


def _capture_metrics(
        tags: Dict[str, str],
        container_metrics_enabled: bool,
        python_metrics_enabled: bool,
        shutdown_event: threading.Event,
        polling_interval: Optional[int] = RUN_METRICS_POLL_INTERVAL_SECONDS,
        logger: Optional[logging.Logger] = None
) -> bool:
    check.dict_param(tags, "tags", str, str)
    check.bool_param(container_metrics_enabled, "container_metrics_enabled")
    check.bool_param(python_metrics_enabled, "python_metrics_enabled")
    check.inst_param(shutdown_event, "shutdown_event", threading.Event)

    if logger:
        logging.info(f"Starting metrics capture thread for {tags}")
        logging.debug(f"  [container_metrics_enabled={container_metrics_enabled}]")
        logging.debug(f"  [python_metrics_enabled={python_metrics_enabled}]")

    while not shutdown_event.is_set():
        try:
            if container_metrics_enabled:
                _report_container_metrics(tags, logger=logger)

            if python_metrics_enabled:
                _report_python_runtime_metrics(tags, logger=logger)
        except:  # noqa: E722
            logging.error("Exception during capture of metrics, will cease capturing for this run", exc_info=True)
            return False  # terminate the thread safely without interrupting the main thread

        sleep(polling_interval)
    return True


def start_run_metrics_thread(
        dagster_run: DagsterRun, logger: Optional[logging.Logger] = None, polling_interval: Optional[int] = None
) -> Tuple[threading.Thread, threading.Event]:
    # check.inst_param(instance_ref, "instance_ref", InstanceRef)
    check.inst_param(dagster_run, "dagster_run", DagsterRun)
    check.opt_inst_param(logger, "logger", logging.Logger)

    # TODO - some feature flagging here
    container_metrics_enabled = _process_is_containerized()
    python_metrics_enabled = True

    # TODO - ensure at least one metrics source is enabled
    assert container_metrics_enabled or python_metrics_enabled, "No metrics enabled"

    tags = _metric_tags(dagster_run)

    if logger:
        logger.info("Starting run metrics thread")

    shutdown_event = threading.Event()
    thread = threading.Thread(
        target=_capture_metrics,
        args=(tags, container_metrics_enabled, python_metrics_enabled, shutdown_event, polling_interval, logger),
        name="run-metrics",
    )
    thread.start()
    return thread, shutdown_event


def stop_run_metrics_thread(
        thread: threading.Thread,
        stop_event: threading.Event,
        timeout: Optional[int] = RUN_METRICS_POLL_INTERVAL_SECONDS
) -> bool:
    thread = check.not_none(thread)
    stop_event = check.not_none(stop_event)

    stop_event.set()
    if thread.is_alive():
        thread.join(timeout=timeout)

    return not thread.is_alive()
