# SPDX-License-Identifier: LGPL-3.0-only
"""Build shim: the C extension cannot be declared in pyproject.toml alone."""
from setuptools import setup, Extension

setup(
    ext_modules=[Extension('atemwire.mediaconvert', ['atemwire/mediaconvertmodule.c'])],
)
