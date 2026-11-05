# SPDX-License-Identifier: MIT
"""The base of ultimattewire's own exceptions, apart so that every module
can import it without importing the others."""


class UltimatteError(Exception):
    """Anything the unit refused, or a session that failed. Catch this to
    catch every ultimattewire protocol error."""
