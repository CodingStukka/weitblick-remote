"""The Linux port lives in the macOS app's repository and is released together with it."""

import re
import subprocess
import unittest
from pathlib import Path
from xml.etree import ElementTree

from weitblick import APP_ID, VERSION

REPOSITORY = Path(__file__).resolve().parents[2]
LINUX = REPOSITORY / "linux"


class RepositoryTests(unittest.TestCase):
    def test_version_matches_the_macos_app(self):
        project = (REPOSITORY / "project.yml").read_text()
        self.assertEqual(set(re.findall(r'MARKETING_VERSION:\s*"([^"]+)"', project)), {VERSION})

    def test_shared_sources_are_there(self):
        self.assertTrue((REPOSITORY / "Sources" / "WeitblickBridge" / "include" / "WeitblickBridge.h").is_file())
        self.assertTrue((REPOSITORY / "design" / "icon" / "app-icon.svg").is_file())

    def test_appstream_release_is_this_version(self):
        metainfo = ElementTree.parse(LINUX / "data" / f"{APP_ID}.metainfo.xml").getroot()
        self.assertEqual(metainfo.find("releases/release").get("version"), VERSION)

    def test_the_flatpak_builds_the_freerdp_of_the_macos_app(self):
        manifest = (LINUX / "flatpak" / f"{APP_ID}.yml").read_text()
        pinned = re.search(r"url: https://github.com/FreeRDP/FreeRDP.git\s+tag: \S+\s+commit: ([0-9a-f]{40})", manifest)
        self.assertIsNotNone(pinned)
        try:
            submodule = subprocess.run(["git", "-C", str(REPOSITORY), "ls-tree", "HEAD", "vendor/FreeRDP"],
                                       capture_output=True, text=True, check=True).stdout.split()
        except (OSError, subprocess.CalledProcessError):
            self.skipTest("not a git checkout (the Flatpak build copies the sources without .git)")
        self.assertEqual(submodule[2], pinned.group(1))


if __name__ == "__main__":
    unittest.main()
