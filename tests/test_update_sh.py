"""Exercise the updater offline with real Git repositories and a Docker stub."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(shutil.which("bash") and shutil.which("git"), "bash and git required")
class TestUpdateSh(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.origin = self.root / "origin.git"
        self.seed = self.root / "seed"
        self.checkout = self.root / "checkout"
        self.log = self.root / "docker.log"
        self.env = os.environ.copy()
        for key in tuple(self.env):
            if key.startswith("GIT_") or key in ("UPDATE_REF", "MONITOR_URL", "BASH_ENV", "ENV"):
                self.env.pop(key)
        self.env.update({
            "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_TERMINAL_PROMPT": "0", "DOCKER_LOG": str(self.log),
        })
        self.git(self.root, "init", "--bare", str(self.origin))
        self.git(self.root, "init", "-b", "master", str(self.seed))
        self.git(self.seed, "config", "user.name", "Test")
        self.git(self.seed, "config", "user.email", "test@example.org")
        shutil.copy2(ROOT / "update.sh", self.seed / "update.sh")
        self.git(self.seed, "add", "update.sh")
        self.git(self.seed, "commit", "-m", "Initial release")
        self.git(self.seed, "tag", "v1.0.0")
        self.git(self.seed, "remote", "add", "origin", str(self.origin))
        self.git(self.seed, "push", "origin", "master", "--tags")
        self.git(self.root, "clone", str(self.origin), str(self.checkout))
        # A local backup also prevents a failed origin fetch from reaching the network.
        self.git(self.checkout, "remote", "add", "backup", str(self.origin))
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        docker = bin_dir / "docker"
        docker.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$DOCKER_LOG"\n')
        docker.chmod(0o755)
        self.env["PATH"] = str(bin_dir) + os.pathsep + self.env["PATH"]

    def git(self, cwd, *args):
        return subprocess.run(["git", *args], cwd=cwd, env=self.env, check=True,
                              capture_output=True, text=True, timeout=15).stdout.strip()

    def publish_release(self):
        (self.seed / "release.txt").write_text("new release\n")
        self.git(self.seed, "add", "release.txt")
        self.git(self.seed, "commit", "-m", "New release")
        self.git(self.seed, "tag", "v1.1.0")
        self.git(self.seed, "push", "origin", "master", "--tags")
        return self.git(self.seed, "rev-parse", "HEAD")

    def update(self, *args):
        return subprocess.run(["bash", "update.sh", *args], cwd=self.checkout,
                              env=self.env, check=True, capture_output=True,
                              text=True, timeout=15).stdout

    def assert_rebuilt(self):
        self.assertEqual(self.log.read_text().splitlines(),
                         ["compose up -d --build", "image prune -f"])

    def test_force_updates_to_newest_release_and_rebuilds(self):
        target = self.publish_release()
        self.update("-f")
        self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), target)
        self.assert_rebuilt()

    def test_force_updates_to_pinned_release(self):
        self.publish_release()
        self.git(self.checkout, "fetch", "--tags", "origin")
        self.git(self.checkout, "reset", "--hard", "v1.1.0")
        self.env["UPDATE_REF"] = "v1.0.0"
        self.update("-f")
        self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"),
                         self.git(self.seed, "rev-parse", "v1.0.0"))
        self.assert_rebuilt()

    def test_force_updates_to_selected_branch(self):
        target = self.publish_release()
        self.env["UPDATE_REF"] = "master"
        self.update("--force")
        self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), target)
        self.assert_rebuilt()

    def test_no_change_does_not_rebuild(self):
        output = self.update()
        self.assertIn("Already on release v1.0.0", output)
        self.assertFalse(self.log.exists())

    def test_force_rebuilds_unchanged_release(self):
        target = self.git(self.checkout, "rev-parse", "HEAD")
        self.update("-f")
        self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), target)
        self.assert_rebuilt()

    def test_changed_release_updates_without_force(self):
        target = self.publish_release()
        self.update()
        self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), target)
        self.assert_rebuilt()


if __name__ == "__main__":
    unittest.main()
