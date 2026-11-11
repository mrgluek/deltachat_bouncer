"""Docker build exclusions must protect local configuration and keep runtime files."""
import fnmatch
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class TestDockerContext(unittest.TestCase):
    def setUp(self):
        self.patterns = [
            line.strip().strip("/")
            for line in (ROOT / ".dockerignore").read_text().splitlines()
            if line.strip() and not line.startswith("#")
        ]

    def excluded(self, path):
        # The current ignore file uses only positive, root-relative globs.
        parts = path.split("/")
        return any(
            fnmatch.fnmatchcase("/".join(parts[:end]), pattern)
            for end in range(1, len(parts) + 1)
            for pattern in self.patterns
        )

    def test_local_configuration_is_excluded(self):
        for path in (
            ".env", ".env.local", ".env.production", "production.env",
            "server.pem", "server.key", "id_ed25519", "id_rsa.pub",
            "credentials.json", "credentials/token", "secrets.json", "secrets/token",
            "bouncer.sqlite", "bouncer.sqlite3", "bouncer.sqlite-wal",
            "bouncer.db", "bouncer.db-wal", "bouncer.db-shm",
            ".claude/settings.json", ".agents/notes.md", "CLAUDE.local.md",
            "GEMINI.md", "PLAN.md", "data/account", ".git/config",
        ):
            with self.subTest(path=path):
                self.assertTrue(self.excluded(path), path)

    def test_runtime_files_are_included(self):
        paths = ["requirements.txt", "icon.png", "background.jpg",
                 "background-light.jpg", "background-light.png"]
        paths.extend(str(path.relative_to(ROOT)) for path in ROOT.glob("*.py"))
        for directory in ("web", "static"):
            paths.extend(str(path.relative_to(ROOT)) for path in (ROOT / directory).rglob("*")
                         if path.is_file() and "__pycache__" not in path.parts
                         and path.suffix not in (".pyc", ".pyo"))  # compiled files are excluded on purpose
        for path in paths:
            with self.subTest(path=path):
                self.assertFalse(self.excluded(path), path)


if __name__ == "__main__":
    unittest.main()
