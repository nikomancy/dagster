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
    return DagsterRun(job_name='test', run_id='123')


@mark.parametrize('test_case, platform_name, is_file, readable, link_path, expected', [
    ("non-linux OS should always evaluate to false", "darwin", True, True, '/sbin/init', False),
    ("standard linux, outside cgroup", "linux", True, True, '/sbin/init', False),
    ("standard linux, container cgroup", "linux", True, True, '/bin/bash', True),
    ("unreadable process file should evaluate to false", "linux", False, False, '', False),
])
def test_process_is_containerized(monkeypatch, test_case, platform_name, is_file, readable, link_path, expected):
    monkeypatch.setattr('os.path.isfile', lambda x: is_file)
    monkeypatch.setattr('os.access', lambda x, y: readable)
    monkeypatch.setattr('os.readlink', lambda x: link_path)

    with patch.object(run_metrics_thread, '_get_platform_name', return_value=platform_name):
        assert run_metrics_thread._process_is_containerized() is expected, test_case  # noqa: SLF001


def test_metric_tags(dagster_run):
    tags = run_metrics_thread._metric_tags(dagster_run)  # noqa: SLF001
    assert tags['job_name'] == 'test'
    assert tags['run_id'] == '123'


def test_python_gc_metrics():
    python_runtime_metrics = run_metrics_thread._get_python_runtime_metrics() # noqa: SLF001

    assert 'gc_stats' in python_runtime_metrics
    assert 'gc_freeze_count' in python_runtime_metrics
    assert isinstance(python_runtime_metrics['gc_stats'], list)
    assert isinstance(python_runtime_metrics['gc_freeze_count'], int)
    assert python_runtime_metrics['gc_stats'][0]["collections"] >= 0


def test_start_run_metrics_thread(dagster_run, caplog):
    logger = logging.getLogger("test_run_metrics")
    logger.setLevel(logging.DEBUG)
    thread, shutdown = run_metrics_thread.start_run_metrics_thread(dagster_run, logger=logger, polling_interval=2)

    assert thread.is_alive()

    time.sleep(1)
    shutdown.set()

    thread.join()
    assert thread.is_alive() is False
    assert "Starting run metrics thread" in caplog.messages[0]
