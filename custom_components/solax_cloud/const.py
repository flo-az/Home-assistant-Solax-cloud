"""Constants for the Solax Cloud integration."""

import logging

LOGGER = logging.getLogger(__package__)

CONF_API_ADDRESS = "api_address"
CONF_TOKEN = "token_id"
CONF_SERIAL = "serial_number"

# Shown on the API page of Solax Cloud; entries from before the v2 API
# have no address stored and use this one.
DEFAULT_API_ADDRESS = "https://global.solaxcloud.com"

DOMAIN = "solax_cloud"
