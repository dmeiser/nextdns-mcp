#!/usr/bin/env python
"""Test patch.dict behavior."""
import os
from unittest.mock import patch

# Test how patch.dict works
env = {}
with patch.dict("os.environ", env, clear=True):
    env["NEW"] = "test"
    print("In context:", os.getenv("NEW"))
    print("Direct access:", os.environ.get("NEW"))
