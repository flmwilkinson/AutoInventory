"""setup.py with side effects — must never be executed or installed."""

import os

os.system("echo pwned > canary2.txt")

from setuptools import setup

setup(name="evil-package", version="0.0.1", py_modules=["evil"])
