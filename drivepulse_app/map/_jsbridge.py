"""Safe builder for MapLibre JS-bridge calls.

Every argument is JSON-encoded before being spliced into the call string.
JSON is a syntactic subset of JavaScript literals, so ``json.dumps`` yields
a correctly-escaped value for str / bool / int / float / list / dict — which
closes the door on a string argument (e.g. a geocoded place name) breaking
out of the call and executing as script in the WebKit view.
"""
from __future__ import annotations

import json


def js_call(fn: str, *args: object) -> str:
    return f"{fn}({','.join(json.dumps(a) for a in args)})"
