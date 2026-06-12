from __future__ import annotations

from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from stratatools import image


class ImagePrepareTests(unittest.TestCase):
    def test_cmd_build_stages_guardian_imagebuild_sources(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            repo = root / "agent"
            context = repo / "backend"
            staged = root / "partitions" / "agent" / "payloads" / "sources" / "lagent-backend"
            context.mkdir(parents=True)

            subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
            (repo / ".gitignore").write_text("backend/node_modules/\n", encoding="utf-8")
            (context / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
            (context / "app.py").write_text("print('ok')\n", encoding="utf-8")
            (context / "local.env").write_text("DEBUG=1\n", encoding="utf-8")
            (context / "node_modules").mkdir()
            (context / "node_modules" / "ignored.js").write_text("ignored\n", encoding="utf-8")
            subprocess.run(["git", "add", ".gitignore", "backend/Dockerfile", "backend/app.py"], cwd=repo, check=True, capture_output=True)

            with mock.patch.object(image, "BUILD_RECIPES", {}), mock.patch.object(
                image,
                "IMAGEBUILD_PREPARE_RECIPES",
                {"agent": [(repo, context, staged)]},
            ):
                image.cmd_build(["agent"], dry_run=False)

            self.assertTrue((staged / "Dockerfile").is_file())
            self.assertTrue((staged / "app.py").is_file())
            self.assertTrue((staged / "local.env").is_file())
            self.assertFalse((staged / "node_modules" / "ignored.js").exists())


if __name__ == "__main__":
    unittest.main()
