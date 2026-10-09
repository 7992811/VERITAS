import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

import sitecustomize as embed


class EmbeddedSignalRobotBootstrapTests(unittest.TestCase):
    def test_default_is_inert(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(
                sys, "argv", ["veritas_intelligence.py"]):
            self.assertFalse(embed.should_start())

    def test_production_http_handler_wires_currency_trade_service(self):
        source = Path("veritas_intelligence.py").read_text(encoding="utf-8")
        self.assertIn("import veritas_currency_trade_service as VCTS", source)
        self.assertIn("VCTS.reply_http(", source)
        self.assertIn("startswith(VCTS.PREFIX)", source)

    def test_requires_exact_web_entrypoint_and_both_flags(self):
        env = {
            "VERITAS_SIGNAL_ROBOT_EMBEDDED": "true",
            "VERITAS_SIGNAL_ROBOT_ENABLED": "true",
        }
        with patch.dict(os.environ, env, clear=True):
            with patch.object(sys, "argv", ["bot.py"]):
                self.assertFalse(embed.should_start())
            with patch.object(sys, "argv", [str(Path("/srv/veritas_intelligence.py"))]):
                self.assertTrue(embed.should_start())


if __name__ == "__main__":
    unittest.main()
