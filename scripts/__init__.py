"""Scripts package root.

Mypy checks ``src/``, ``scripts/`` and ``tests/`` in one run. This package root
gives those script modules one identity for the ``scripts.*`` imports used by
the test suite and the container runner; the scripts themselves keep their
historical top-level imports when run directly as files.
"""
