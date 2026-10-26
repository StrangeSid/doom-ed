# SPDX-License-Identifier: GPL-3.0-or-later
"""Optional school integrations. Local study works with zero plugins."""

from app.integrations.base import (
    IntegrationNotConnectedError,
    IntegrationUpstreamError,
    LearningContext,
    LMSPlugin,
)
from app.integrations.indus import IndusLMSPlugin
from app.integrations.registry import (
    active_plugin,
    all_plugins,
    available_plugins,
    build_learning_context,
    get,
    register,
    status,
)

# The built-in Indus adapter is registered but remains optional: disabling
# DOOMED_LMS_PLUGIN or removing the upstream package makes it unavailable.
register(IndusLMSPlugin())

__all__ = [
    "IndusLMSPlugin",
    "IntegrationNotConnectedError",
    "IntegrationUpstreamError",
    "LMSPlugin",
    "LearningContext",
    "active_plugin",
    "all_plugins",
    "available_plugins",
    "build_learning_context",
    "get",
    "register",
    "status",
]
