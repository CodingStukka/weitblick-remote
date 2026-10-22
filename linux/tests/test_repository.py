"""The Linux port lives in the macOS app's repository and is released together with it."""

import re
import unittest
from pathlib import Path

from weitblick import VERSION

REPOSITORY = Path(__file__).resolve().parents[2]


class RepositoryTests(unittest.TestCase):
    def test_version_matches_the_macos_app(self):
        project = (REPOSITORY / "project.yml").read_text()
        self.assertEqual(set(re.findall(r'MARKETING_VERSION:\s*"([^"]+)"', project)), {VERSION})

    def test_shared_sources_are_there(self):
        self.assertTrue((REPOSITORY / "Sources" / "WeitblickBridge" / "include" / "WeitblickBridge.h").is_file())
        self.assertTrue((REPOSITORY / "design" / "icon" / "app-icon.svg").is_file())


if __name__ == "__main__":
    unittest.main()
