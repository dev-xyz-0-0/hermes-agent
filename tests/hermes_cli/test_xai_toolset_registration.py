"""Regression tests for xAI web-search toolset registration."""

import model_tools
from hermes_cli.tools_config import CONFIGURABLE_TOOLSETS
from toolsets import validate_toolset


def test_xai_web_search_toolset_is_registered_and_configurable():
    assert validate_toolset("xai_web_search") is True

    configured_names = {
        name for name, _label, _description in CONFIGURABLE_TOOLSETS
    }
    assert "x_search" not in configured_names
