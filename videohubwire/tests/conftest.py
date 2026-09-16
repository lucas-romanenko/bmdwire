# SPDX-License-Identifier: MIT
import videohubwire


def pytest_report_header(config):
    return f"videohubwire imported from {videohubwire.__file__}"
