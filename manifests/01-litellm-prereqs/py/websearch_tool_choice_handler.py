"""Work around forced web-search follow-ups in LiteLLM 1.106.0."""

from pathlib import Path

import yaml
from litellm.integrations.websearch_interception.handler import (
    WebSearchInterceptionLogger,
)
from litellm.integrations.websearch_interception.tools import (
    LITELLM_WEB_SEARCH_TOOL_NAME,
)


class WebSearchToolChoiceHandler(WebSearchInterceptionLogger):
    async def _build_anthropic_request_patch(self, *args, **kwargs):
        patch, outcomes = await super()._build_anthropic_request_patch(*args, **kwargs)
        choice = patch.kwargs.get("tool_choice", patch.optional_params.get("tool_choice"))
        if isinstance(choice, dict) and (
            choice.get("type") == "any"
            or (
                choice.get("type") == "tool"
                and choice.get("name") == LITELLM_WEB_SEARCH_TOOL_NAME
            )
        ):
            # The dispatcher merges this patch into the original parameters.
            # Omitting tool_choice would leave the original forced choice intact.
            patch.optional_params["tool_choice"] = {
                **{key: value for key, value in choice.items() if key != "name"},
                "type": "auto",
            }
            # Avoid overriding auto (or passing the same keyword twice).
            patch.kwargs.pop("tool_choice", None)
        return patch, outcomes


# The custom callback loader does not pass litellm_settings to the instance.
# Read the same config mounted next to this module when the callback is loaded.
with Path(__file__).with_name("config.yaml").open(encoding="utf-8") as config_file:
    search_config = yaml.safe_load(config_file)["litellm_settings"][
        "websearch_interception_params"
    ]

handler = WebSearchToolChoiceHandler.from_config_yaml(search_config)
