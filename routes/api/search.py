"""Jump-to search — WP-4 D8.

One endpoint over `search_index`. The ranking and the index both live in that
module; this is the HTTP shape and nothing else, which is what makes both
testable without a request.
"""

from flask import current_app, jsonify, request

import search_index

from ._shared import api_bp


@api_bp.route("/search")
def jump_search():
    """Jump targets for a query.

    Returns `[]` for an empty query rather than the whole index: a search box
    that dumps eighty destinations the moment it is focused has answered a
    question nobody asked, and on a fleet this size the list is longer than
    the screen.
    """
    query = (request.args.get("q") or "").strip()
    if not query:
        return jsonify({"ok": True, "results": []})

    settings = current_app.extensions.get("prism_config")
    lang = "en"
    try:
        import app as prism_app
        lang = prism_app.config.get_settings().get("language", "en")
    except Exception:
        pass

    index = search_index.get(current_app._get_current_object(), lang)
    return jsonify({
        "ok": True,
        "results": search_index.search(index, query),
        "indexed": len(index),
    })
