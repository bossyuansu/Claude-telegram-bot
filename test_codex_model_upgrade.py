"""Model upgrades preserve session choices and the existing quota policy."""

import os
import subprocess
import sys
import unittest
from unittest import mock

import bot


class TestCodexModelUpgrade(unittest.TestCase):
    def setUp(self):
        self.session = {"id": "model-test", "name": "model-test", "cwd": "/tmp"}
        patches = {
            "get_active_session": mock.Mock(return_value=self.session),
            "save_sessions": mock.Mock(),
            "send_message": mock.Mock(),
            "_codex_usage": mock.Mock(return_value=None),
        }
        for name, value in patches.items():
            patcher = mock.patch.object(bot, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(lambda: setattr(bot._codex_model_ctx, "model", None))

    def select(self, name):
        self.assertTrue(bot.handle_command(12345, f"/model codex {name}"))

    def test_latest_and_legacy_choices_reach_the_runtime_selector(self):
        choices = {
            "astra": "gpt-6.2-astra", "gpt6": "gpt-6.2-astra",
            "gpt-6.2-astra": "gpt-6.2-astra", "sol": "gpt-6.2-sol",
            "gpt-6.2-sol": "gpt-6.2-sol", "astra-6": "gpt-6-astra",
            "gpt-6-astra": "gpt-6-astra", "sol-6.1": "gpt-6.1-sol",
            "gpt-6.1-sol": "gpt-6.1-sol", "sol-6": "gpt-6-sol",
            "gpt-6-sol": "gpt-6-sol", "sol-5.6": "gpt-5.6-sol",
            "gpt-5.6-sol": "gpt-5.6-sol", "5.5": "gpt-5.5",
            "gpt-5.5": "gpt-5.5",
        }
        for name, expected in choices.items():
            with self.subTest(name=name):
                bot.save_sessions.reset_mock()
                self.select(name)
                self.assertEqual(self.session["codex_model_override"], expected)
                self.assertEqual(bot._codex_model(), expected)
                bot.save_sessions.assert_called_once_with(force=True)
                self.assertIn(expected, bot.send_message.call_args.args[1])

    def test_clearing_a_selection_restores_the_new_default(self):
        self.select("sol-6.1")
        self.select("default")
        self.assertNotIn("codex_model_override", self.session)
        self.assertEqual(bot._codex_model(), "gpt-6.2-astra")

    def test_invalid_selection_preserves_the_previous_choice(self):
        self.select("sol")
        bot.save_sessions.reset_mock()
        self.select("unknown-model")
        self.assertEqual(bot._codex_model(), "gpt-6.2-sol")
        bot.save_sessions.assert_not_called()
        message = bot.send_message.call_args.args[1]
        self.assertIn("gpt-6.2-astra", message)
        self.assertIn("gpt-6.2-sol", message)

    def test_both_astra_versions_use_the_new_sol_fallback(self):
        for astra in ("astra", "astra-6"):
            for usage in (
                {"primary": {"used_percent": bot.CODEX_USAGE_FALLBACK_PERCENT}},
                {"rate_limit_reached_type": "primary"},
            ):
                with self.subTest(astra=astra, usage=usage):
                    self.select(astra)
                    bot._codex_usage.return_value = usage
                    self.assertEqual(bot._codex_model(), "gpt-6.2-sol")
        self.select("sol-6.1")
        self.assertEqual(bot._codex_model(), "gpt-6.1-sol")

    def test_environment_configuration_still_overrides_defaults(self):
        env = dict(os.environ, CODEX_MODEL="gpt-6-astra", CODEX_FALLBACK_MODEL="gpt-6.1-sol")
        result = subprocess.run(
            [sys.executable, "-c", "import bot; print(bot.CODEX_MODEL, bot.CODEX_FALLBACK_MODEL)"],
            cwd=os.path.dirname(bot.__file__), env=env, capture_output=True, text=True, check=True,
        )
        self.assertEqual(result.stdout.strip(), "gpt-6-astra gpt-6.1-sol")


if __name__ == "__main__":
    unittest.main()
