"""Example suite for `run_test_suite`: checks the bundled fake-device fixtures.

In a real project the configured suite would be something like
``./gradlew connectedDebugAndroidTest`` or ``pytest tests/appium``. This one is
small and self-contained so the demo can run it anywhere. It writes a JUnit XML
report that the server summarises.
"""

from __future__ import annotations

import json
import os
import sys
import time
import unittest
from pathlib import Path
from xml.sax.saxutils import quoteattr

from qa_mcp_server.fake import FakeAdbTransport, bundled_scenario_path
from qa_mcp_server.uitree import parse_hierarchy


class FixtureSmoke(unittest.TestCase):
    def test_scenario_is_valid_json(self) -> None:
        data = json.loads(bundled_scenario_path().read_text(encoding="utf-8"))
        self.assertIn("screens", data)

    def test_every_screen_parses(self) -> None:
        fake = FakeAdbTransport.from_file()
        for name in fake.screens:
            self.assertTrue(parse_hierarchy(fake.render(name)), name)

    def test_screenshot_is_png(self) -> None:
        self.assertTrue(FakeAdbTransport.from_file().screenshot.startswith(b"\x89PNG"))


def main() -> int:
    print(f"ANDROID_SERIAL={os.environ.get('ANDROID_SERIAL', '<unset>')}")
    start = time.perf_counter()
    result = unittest.TextTestRunner(stream=sys.stdout, verbosity=2).run(
        unittest.defaultTestLoader.loadTestsFromTestCase(FixtureSmoke)
    )
    elapsed = time.perf_counter() - start
    failures = len(result.failures)
    errors = len(result.errors)
    with Path("smoke-junit.xml").open("w", encoding="utf-8") as fh:
        fh.write(
            f'<testsuite name={quoteattr("smoke")} tests="{result.testsRun}" '
            f'failures="{failures}" errors="{errors}" skipped="{len(result.skipped)}" '
            f'time="{elapsed:.3f}"/>\n'
        )
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
