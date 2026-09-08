import unittest
from unittest.mock import Mock

import screenshot_app as app


class FakeScheduler:
    def __init__(self) -> None:
        self.jobs = {}
        self.next_id = 0

    def after(self, delay_ms, callback):
        del delay_ms
        self.next_id += 1
        job_id = f"job-{self.next_id}"
        self.jobs[job_id] = callback
        return job_id

    def after_cancel(self, job_id) -> None:
        self.jobs.pop(job_id, None)

    def run_next(self) -> None:
        job_id = next(iter(self.jobs))
        callback = self.jobs.pop(job_id)
        callback()


class WindowsPhysicalKeyMonitorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.scheduler = FakeScheduler()
        self.state = {}
        self.callback = Mock()
        self.monitor = app.WindowsPhysicalKeyMonitor(
            self.scheduler,
            ord("C"),
            self.callback,
            active_when=lambda: True,
            key_state=lambda key: bool(self.state.get(key, False)),
        )

    def test_physical_press_triggers_once_while_key_is_held(self) -> None:
        self.assertTrue(self.monitor.start())

        self.state[ord("C")] = True
        self.scheduler.run_next()
        self.scheduler.run_next()

        self.callback.assert_called_once_with()

    def test_release_allows_the_next_press(self) -> None:
        self.monitor.start()
        self.state[ord("C")] = True
        self.scheduler.run_next()
        self.state[ord("C")] = False
        self.scheduler.run_next()
        self.state[ord("C")] = True
        self.scheduler.run_next()

        self.assertEqual(self.callback.call_count, 2)

    def test_ctrl_c_is_not_treated_as_bare_color_copy(self) -> None:
        self.monitor.start()
        self.state[self.monitor.VK_CONTROL] = True
        self.state[ord("C")] = True
        self.scheduler.run_next()
        self.state[self.monitor.VK_CONTROL] = False
        self.scheduler.run_next()

        self.callback.assert_not_called()

    def test_press_outside_active_window_has_no_late_trigger(self) -> None:
        active = False
        monitor = app.WindowsPhysicalKeyMonitor(
            self.scheduler,
            ord("C"),
            self.callback,
            active_when=lambda: active,
            key_state=lambda key: bool(self.state.get(key, False)),
        )
        monitor.start()
        self.state[ord("C")] = True
        self.scheduler.run_next()
        active = True
        self.scheduler.run_next()

        self.callback.assert_not_called()

    def test_stop_cancels_pending_poll(self) -> None:
        self.monitor.start()

        self.monitor.stop()

        self.assertEqual(self.scheduler.jobs, {})


if __name__ == "__main__":
    unittest.main()
