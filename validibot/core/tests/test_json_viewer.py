"""Security tests for the reusable JSON viewer template.

JSON shown by the run and workflow export pages can contain user-controlled
strings. These tests ensure that such content remains inert data even when it
contains an HTML script-closing sequence.
"""

import json

from django.template.loader import render_to_string
from lxml import html as lxml_html


def test_json_viewer_escapes_script_closing_sequences():
    """A JSON string must not break out of the inert data script element."""
    payload = {
        "name": '</script><form id="json-injection-marker"></form><script>',
    }

    rendered = render_to_string(
        "app/partial/components/json_viewer.html",
        {"json_data": payload, "CSP_NONCE": "test-nonce"},
    )
    document = lxml_html.fromstring(rendered)
    data_element = document.get_element_by_id("json-raw-data")

    assert json.loads(data_element.text) == payload
    assert not document.xpath('//*[@id="json-injection-marker"]')
    assert "\\u003C/script\\u003E" in rendered
