# tools/__init__.py
"""Package marker — lets mypy resolve tools as a package.

Without it mypy infers the module name from the file path and refuses to
build a package hierarchy (`--explicit-package-bases` works around it, but a
marker is the real fix). It also makes `python -c "import tools"` behave the
same from any working directory.
"""
