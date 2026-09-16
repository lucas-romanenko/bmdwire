# SPDX-License-Identifier: LGPL-3.0-only
"""Build shim: the C extension cannot be declared in pyproject.toml alone.

atemwire.mediaconvert does the BT.709 conversion and RLE encoding for
media-pool transfers. Everything else about the distribution is in
pyproject.toml.
"""
from setuptools import setup, Extension

setup(
    ext_modules=[Extension('atemwire.mediaconvert', ['atemwire/mediaconvertmodule.c'])],
)
