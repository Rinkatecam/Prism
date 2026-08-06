"""routes.api package — slim shim that wires sub-blueprints together.

The original 5050-line routes/api.py was split into per-domain modules
under this package. This __init__ imports every submodule (so each
@api_bp.route is registered on the single api blueprint), defines
register_api_routes() applying rate limits in one place, and
re-exports symbols imported by external code (notably
_update_install_state, used by routes.views).
"""

from database import Database
from config_manager import ConfigManager

from . import _shared
from ._shared import api_bp, _set_state, _update_install_state  # noqa: F401  (re-export)

# Importing every sub-module triggers its @api_bp.route decorations so all
# endpoints get attached to the single api_bp blueprint.
from . import servers as _servers          # noqa: F401
from . import metrics as _metrics          # noqa: F401
from . import config as _config_mod        # noqa: F401
from . import updates as _updates          # noqa: F401
from . import power as _power              # noqa: F401
from . import workflows as _workflows      # noqa: F401
from . import rbac as _rbac                # noqa: F401
from . import reports as _reports          # noqa: F401
from . import health as _health            # noqa: F401
from . import misc as _misc                # noqa: F401
# CSV / compliance dashboard endpoints — feature-flagged via
# settings.compliance.enabled, so a deployment that doesn't need them
# can leave the flag off and never see the surface.
from . import compliance as _compliance    # noqa: F401


def register_api_routes(app, db: Database, config: ConfigManager, limiter=None):
    """Wire shared state, apply rate limits, and register the api blueprint.

    Public entry point — kept compatible with
    ``from routes.api import register_api_routes`` in app.py.
    """
    _set_state(db, config, limiter)
    if limiter:
        # Rate limits — preserved verbatim from the original api.py.
        limiter.limit("20 per minute")(_config_mod.save_config)
        limiter.limit("10 per minute")(_config_mod.test_connection)
        limiter.limit("5 per minute")(_config_mod.test_email)
        # /api/restart now caps at 5/hour (S2-3 from AUDIT-2026-05). Operators
        # don't need to bounce the Flask process more often than that, and
        # the previous 2/min ceiling allowed the dashboard to be hammered
        # offline as a detection-suppression tactic.
        limiter.limit("5 per hour")(_power.restart_server)
        limiter.limit("2 per minute")(_power.server_power_action)
        limiter.limit("1 per 5 minutes")(_updates.install_server_updates)
        limiter.limit("5 per minute")(_power.server_wake_on_lan)
    app.register_blueprint(api_bp)
