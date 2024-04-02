import gc
import logging
import os
import os.path
import threading
from datetime import datetime
from sys import platform
from time import sleep
from typing import Dict, Optional, Tuple

import dagster._check as check
from dagster._core.instance import DagsterInstance
from dagster._core.storage.dagster_run import DagsterRun
from dagster._utils.container import (
    retrieve_containerized_utilization_metrics,
)

RUN_METRICS_POLL_INTERVAL_SECONDS = 60


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


def _metric_tags(instance: DagsterInstance, dagster_run: DagsterRun) -> Dict[str, str]:
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
        "job_origin_id": dagster_run.job_code_origin,
        "location_name": location_name,
        "org_name": org_name,
        # "job_origin_id": dagster_run.job_origin_id,
        # "repository_name": dagster_run.repository_name,
        "run_id": dagster_run.run_id
    }


def _get_container_metrics(logger: Optional[logging.Logger] = None) -> Dict[str, float]:
    metrics = retrieve_containerized_utilization_metrics(logger=logger)

    # calculate cpu_limit
    cpu_quota_us = metrics.get("cpu_cfs_quota_us")
    cpu_period_us = metrics.get("cpu_cfs_period_us")
    cpu_usage_ms = metrics.get("cpu_usage")
    cpu_limit_ms = None
    if cpu_quota_us > 0 and cpu_period_us > 0:
        cpu_limit_ms = (cpu_quota_us / cpu_period_us) * 1000

    cpu_percent = None
    if cpu_limit_ms > 0 and cpu_usage_ms > 0:
        cpu_percent = 100.0 * cpu_usage_ms / cpu_limit_ms

    memory_percent = None
    if metrics.get("memory_limit") > 0 and metrics.get("memory_usage") > 0:
        memory_percent = 100.0 * metrics.get("memory_usage") / metrics.get("memory_limit")

    return {
        "container.cpu_usage_ms": metrics.get("cpu_usage"),
        "container.cpu_cfs_period_us": metrics.get("cpu_cfs_period_us"),
        "container.cpu_cfs_quota_us": metrics.get("cpu_cfs_quota_us"),
        "container.cpu_limit_ms": cpu_limit_ms,
        "container.cpu_percent": cpu_percent,
        "container.memory_usage": metrics.get("memory_usage"),
        "container.memory_limit": metrics.get("memory_limit"),
        "container.memory_percent": memory_percent,
    }


def _get_python_runtime_metrics() -> Dict[str, float]:
    gc_stats = gc.get_stats()

    stats_dict = {}
    for generation in gc_stats:
        gen_dict = {f"gc_gen{generation}_{key}": value for key, value in generation.items()}
        stats_dict.update(gen_dict)

    return {
        **stats_dict,
        "gc_freeze_count": gc.get_freeze_count()
    }


def _report_run_metrics(
        instance: DagsterInstance,
        dagster_run: DagsterRun,
        start_time: datetime,
        measurement_time: datetime,
        metrics: Dict[str, float],
        run_tags: Dict[str, str],
        logger: Optional[logging.Logger] = None
):
    cpu_usage_str = f"{metrics.get('container.cpu_usage_ms')} ms/sec" # ({metrics.get('container.cpu_percent'):.2f}%)"
    memory_usage_str = f"{metrics.get('container.memory_usage') / 1048576:.2f} MiB" # ({metrics.get('container.memory_percent'):.2f}%)"

    usage_percentage_strings = []
    if metrics.get("container.cpu_percent") is not None:
        usage_percentage_strings.append(f"CPU: {metrics.get('container.cpu_percent'):.2f} %")
    if metrics.get("container.memory_percent") is not None:
        usage_percentage_strings.append(f"MEMORY: {metrics.get('container.memory_percent'):.2f} %")

    message = f"Container resource usage [CPU: {cpu_usage_str}, MEMORY: {memory_usage_str}]"

    if len(usage_percentage_strings) > 0:
        message += f"[{', '.join(usage_percentage_strings)}]"

    instance.report_engine_event(
        message=message,
        dagster_run=dagster_run,
        run_id=dagster_run.run_id,
    )

    if metrics.get("container.memory_percent") > 75:
        gc_metrics = {key: value for key, value in metrics.items() if key.startswith("gc_")}
        instance.report_engine_event(
            message=f"Python garbage collection metrics: {gc_metrics}",
            dagster_run=dagster_run,
            run_id=dagster_run.run_id,
        )


def _capture_metrics(
        instance: DagsterInstance,
        dagster_run: DagsterRun,
        container_metrics_enabled: bool,
        python_metrics_enabled: bool,
        shutdown_event: threading.Event,
        polling_interval: Optional[int] = RUN_METRICS_POLL_INTERVAL_SECONDS,
        logger: Optional[logging.Logger] = None
) -> bool:
    check.inst_param(instance, "instance", DagsterInstance)
    check.inst_param(dagster_run, "dagster_run", DagsterRun)
    check.bool_param(container_metrics_enabled, "container_metrics_enabled")
    check.bool_param(python_metrics_enabled, "python_metrics_enabled")
    check.inst_param(shutdown_event, "shutdown_event", threading.Event)
    check.opt_int_param(polling_interval, "polling_interval")
    check.opt_inst_param(logger, "logger", logging.Logger)

    if not (container_metrics_enabled or python_metrics_enabled):
        raise ValueError("No metrics enabled")

    start_time = datetime.now()
    run_tags = _metric_tags(instance, dagster_run)

    if logger:
        logging.debug(f"Starting metrics capture thread with tags: {run_tags}")
        logging.debug(f"  [container_metrics_enabled={container_metrics_enabled}]")
        logging.debug(f"  [python_metrics_enabled={python_metrics_enabled}]")

    while not shutdown_event.is_set():
        try:
            metrics = {}

            measurement_time = datetime.now()
            if container_metrics_enabled:
                container_metrics = _get_container_metrics(logger=logger)
                metrics.update(container_metrics)

            if python_metrics_enabled:
                python_metrics = _get_python_runtime_metrics()
                metrics.update(python_metrics)

            if len(metrics) > 0:
                _report_run_metrics(
                    instance,
                    dagster_run=dagster_run,
                    start_time=start_time,
                    measurement_time=measurement_time,
                    metrics=metrics,
                    run_tags=run_tags,
                    logger=logger
                )
        except:  # noqa: E722
            logging.error("Exception during capture of metrics, will cease capturing for this run", exc_info=True)
            return False  # terminate the thread safely without interrupting the main thread

        sleep(polling_interval)
    if logger:
        logging.debug("Shutting down metrics capture thread")
    return True


def start_run_metrics_thread(
        instance: DagsterInstance,
        dagster_run: DagsterRun,
        logger: Optional[logging.Logger] = None,
        polling_interval: Optional[int] = None
) -> Tuple[threading.Thread, threading.Event]:
    check.inst_param(instance, "instance", DagsterInstance)
    check.inst_param(dagster_run, "dagster_run", DagsterRun)
    check.opt_inst_param(logger, "logger", logging.Logger)
    check.opt_int_param(polling_interval, "polling_interval")

    # TODO - some feature flagging here
    container_metrics_enabled = _process_is_containerized()
    python_metrics_enabled = True

    # TODO - ensure at least one metrics source is enabled
    assert container_metrics_enabled or python_metrics_enabled, "No metrics enabled"

    if logger:
        logger.debug("Starting run metrics thread")

    instance.report_engine_event(
        f"Starting run metrics thread with container_metrics_enabled={container_metrics_enabled} and python_metrics_enabled={python_metrics_enabled}",
        dagster_run=dagster_run,
        run_id=dagster_run.run_id,
    )

    shutdown_event = threading.Event()
    thread = threading.Thread(
        target=_capture_metrics,
        args=(instance, dagster_run, container_metrics_enabled, python_metrics_enabled, shutdown_event, polling_interval, logger),
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
