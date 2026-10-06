# Home-assistant-Solax-cloud
Solax cloud integration for home assistant


todo things:
1. Get inverter type and based on model add the correct sensor to the integration.
2. Translations beyond English.

goal:
add integration to the official home assistant integration list.

usage:
1. Use the PocketLAN or PocketWiFi serial number, NOT the inverter serial number.
2. In Solax Cloud, open the API page (API button at the top of the page).
3. Copy the API address and the token ID from there.

The integration uses the v2 API. If Home Assistant asks for a new token after
an update, the stored token only worked with the old v1 API: copy the current
one from the API page.

Installation:
Add repository URL to custom repository in hacs
See: https://hacs.xyz/docs/faq/custom_repositories/

Development:
1. Create a Python 3.14 virtual environment.
2. pip install -r requirements_test.txt
3. pytest
