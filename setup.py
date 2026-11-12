"""Copy the canonical artwork into installed Python packages."""

from pathlib import Path

from setuptools import setup
from setuptools.command.build_py import build_py


class BuildWithIcons(build_py):
    def run(self):
        super().run()
        source = Path(__file__).resolve().parent / "icons"
        destination = Path(self.build_lib) / "grihalekha" / "assets" / "icons"
        self.mkpath(str(destination))
        for name in ("grihalekha.ico", "grihalekha-icon-512.png"):
            self.copy_file(str(source / name), str(destination / name))


setup(cmdclass={"build_py": BuildWithIcons})
