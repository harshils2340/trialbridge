"""Flask wiring for the copilot: a single JSON endpoint the right rail calls.

Kept out of the monolithic ``app.py`` on purpose - the copilot is a self-contained
package. ``register(app)`` attaches the route. Auth/scoping ride on the existing
``g.user`` (study-team) that ``app.py`` already sets per request.
"""

from flask import g, jsonify, request

from . import agent


def register(app):
    @app.route("/app/copilot/ask", methods=["POST"], endpoint="copilot_ask")
    def copilot_ask():
        user = getattr(g, "user", None)
        if not user:
            return jsonify({"ok": False, "error": "auth_required"}), 401
        data = request.get_json(silent=True) or {}
        query = (data.get("q") or "").strip()
        if not query:
            return jsonify({"ok": False, "error": "empty"}), 400
        context = {}
        lead_id = data.get("lead_id")
        if lead_id:
            try:
                context["lead_id"] = int(lead_id)
            except (TypeError, ValueError):
                pass
        try:
            result = agent.answer(user["id"], query, context)
        except Exception:
            return jsonify({"ok": False,
                            "error": "I hit a problem answering that."}), 500
        result["ok"] = True
        return jsonify(result)

    return app
