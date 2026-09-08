import unittest

import screenshot_app as app


class MainWindowVisibilityPolicyTests(unittest.TestCase):
    def test_default_configuration_starts_in_available_tray(self) -> None:
        config = app.PersistedAppConfig()

        self.assertEqual(
            app.MainWindowVisibilityPolicy.startup_state(config, tray_available=True),
            "withdrawn",
        )

    def test_startup_falls_back_to_window_when_tray_is_unavailable(self) -> None:
        config = app.PersistedAppConfig(start_in_tray=True)

        self.assertEqual(
            app.MainWindowVisibilityPolicy.startup_state(config, tray_available=False),
            "normal",
        )

    def test_default_capture_completion_stays_in_tray(self) -> None:
        config = app.PersistedAppConfig(show_main_after_capture=False)

        self.assertEqual(
            app.MainWindowVisibilityPolicy.after_capture_state(
                config,
                previous_state="normal",
                tray_available=True,
            ),
            "withdrawn",
        )

    def test_capture_can_be_configured_to_show_window_every_time(self) -> None:
        config = app.PersistedAppConfig(show_main_after_capture=True)

        self.assertEqual(
            app.MainWindowVisibilityPolicy.after_capture_state(
                config,
                previous_state="withdrawn",
                tray_available=True,
            ),
            "normal",
        )

    def test_non_capture_operation_does_not_change_window_state(self) -> None:
        config = app.PersistedAppConfig()

        self.assertIsNone(
            app.MainWindowVisibilityPolicy.after_capture_state(
                config,
                previous_state=None,
                tray_available=True,
            )
        )

    def test_capture_restores_previous_state_without_tray(self) -> None:
        config = app.PersistedAppConfig(show_main_after_capture=False)

        self.assertEqual(
            app.MainWindowVisibilityPolicy.after_capture_state(
                config,
                previous_state="iconic",
                tray_available=False,
            ),
            "iconic",
        )

    def test_error_forces_main_window_visible(self) -> None:
        config = app.PersistedAppConfig(show_main_after_capture=False)

        self.assertEqual(
            app.MainWindowVisibilityPolicy.after_capture_state(
                config,
                previous_state="withdrawn",
                tray_available=True,
                force_show=True,
            ),
            "normal",
        )


if __name__ == "__main__":
    unittest.main()
