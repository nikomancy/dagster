import logging
import time
from unittest.mock import patch

import dagster._core.execution.run_metrics_thread as run_metrics_thread
from dagster import DagsterInstance, DagsterRun
from pytest import fixture, mark


@fixture()
def dagster_instance():
    return DagsterInstance.ephemeral()


@fixture()
def dagster_run():
    return DagsterRun(job_name="test", run_id="123")


@fixture()
def mock_container_metrics():
    return {
        "container.cpu_usage_ms": 50,
        "container.cpu_cfs_period_us": 10000,
        "container.cpu_cfs_quota_us": 1000,
        "container.cpu_limit_ms": 100,
        "container.cpu_percent": 50.0,
        "container.memory_usage": 16384,
        "container.memory_limit": 65536,
        "container.memory_percent": 25,
    }


@fixture()
def mock_containerized_utilization_metrics():
    return {
        "cpu_usage": 50,
        "cpu_cfs_quota_us": 1000,
        "cpu_cfs_period_us": 10000,
        "memory_usage": 16384,
        "memory_limit": 65536,
    }


def test_get_container_metrics(mock_containerized_utilization_metrics):
    with patch(
        "dagster._core.execution.run_metrics_thread.retrieve_containerized_utilization_metrics",
        return_value=mock_containerized_utilization_metrics,
    ):
        metrics = run_metrics_thread._get_container_metrics()  # noqa: SLF001

        assert metrics
        assert isinstance(metrics, dict)
        assert metrics["container.cpu_usage_ms"] == 50
        assert metrics["container.cpu_limit_ms"] == 100
        assert metrics["container.cpu_cfs_period_us"] == 10000
        assert metrics["container.cpu_cfs_quota_us"] == 1000
        assert metrics["container.memory_usage"] == 16384
        assert metrics["container.memory_limit"] == 65536
        assert metrics["container.memory_percent"] == 25


def test_get_container_metrics_missing_limits(mock_containerized_utilization_metrics):
    del mock_containerized_utilization_metrics["cpu_cfs_quota_us"]
    del mock_containerized_utilization_metrics["cpu_cfs_period_us"]
    del mock_containerized_utilization_metrics["memory_limit"]

    with patch(
        "dagster._core.execution.run_metrics_thread.retrieve_containerized_utilization_metrics",
        return_value=mock_containerized_utilization_metrics,
    ):
        # should not throw
        metrics = run_metrics_thread._get_container_metrics()  # noqa: SLF001

        assert metrics
        assert isinstance(metrics, dict)
        assert metrics["container.cpu_usage_ms"] == 50
        assert metrics["container.cpu_percent"] is None
        assert metrics["container.cpu_limit_ms"] is None
        assert metrics["container.cpu_cfs_period_us"] is None
        assert metrics["container.cpu_cfs_quota_us"] is None
        assert metrics["container.memory_usage"] == 16384
        assert metrics["container.memory_limit"] is None
        assert metrics["container.memory_percent"] is None


@mark.parametrize(
    "test_case, platform_name, is_file, readable, link_path, expected",
    [
        ("non-linux OS should always evaluate to false", "darwin", True, True, "/sbin/init", False),
        ("standard linux, outside cgroup", "linux", True, True, "/sbin/init", False),
        ("standard linux, container cgroup", "linux", True, True, "/bin/bash", True),
        ("unreadable process file should evaluate to false", "linux", False, False, "", False),
    ],
)
def test_process_is_containerized(
    monkeypatch, test_case, platform_name, is_file, readable, link_path, expected
):
    monkeypatch.setattr("os.path.isfile", lambda x: is_file)
    monkeypatch.setattr("os.access", lambda x, y: readable)
    monkeypatch.setattr("os.readlink", lambda x: link_path)

    with patch.object(run_metrics_thread, "_get_platform_name", return_value=platform_name):
        assert run_metrics_thread._process_is_containerized() is expected, test_case  # noqa: SLF001


def test_metric_tags(dagster_instance, dagster_run):
    tags = run_metrics_thread._metric_tags(dagster_instance, dagster_run)  # noqa: SLF001
    assert tags["job_name"] == "test"
    assert tags["run_id"] == "123"


def test_python_gc_metrics():
    python_runtime_metrics = run_metrics_thread._get_python_runtime_metrics()  # noqa: SLF001

    assert "python.runtime.gc_gen_0.collections" in python_runtime_metrics
    assert "python.runtime.gc_freeze_count" in python_runtime_metrics
    assert isinstance(python_runtime_metrics["python.runtime.gc_gen_0.collections"], int)
    assert isinstance(python_runtime_metrics["python.runtime.gc_freeze_count"], int)
    assert python_runtime_metrics["python.runtime.gc_gen_0.collections"] >= 0


def test_start_run_metrics_thread(dagster_instance, dagster_run, mock_container_metrics, caplog):
    logger = logging.getLogger("test_run_metrics")
    logger.setLevel(logging.DEBUG)

    with patch(
        "dagster._core.execution.run_metrics_thread._get_container_metrics",
        return_value=mock_container_metrics,
    ):
        with patch(
            "dagster._core.execution.run_metrics_thread._process_is_containerized",
            return_value=True,
        ):
            thread, shutdown = run_metrics_thread.start_run_metrics_thread(
                dagster_instance,
                dagster_run,
                logger=logger,
                container_metrics_enabled=True,
                polling_interval=2,
            )

            assert thread.is_alive()

            time.sleep(1)
            shutdown.set()

            thread.join()
            assert thread.is_alive() is False
            assert "Starting run metrics thread" in caplog.messages[0]
