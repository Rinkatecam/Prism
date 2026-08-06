"""Data classes for the Prism monitoring system."""

from dataclasses import dataclass, field
from enum import Enum
from crypto_utils import PASSWORD_MASK


class ServerStatus(str, Enum):
    """Canonical server status values used across the collector → DB → UI path.

    Subclassing `str` means existing code paths that compare against literal
    strings (`status == "healthy"`) keep working — `ServerStatus.HEALTHY` IS
    a `str` whose value is `"healthy"`. The enum exists to give us a single
    source of truth and to make typos at the call site crash at import time
    instead of silently creating a new state.

    Order from "best" to "worst" matches the precedence used by the status
    decision tree in collector.py.
    """

    HEALTHY = "healthy"
    QUEUED = "queued"        # Operator-initiated change in flight (e.g. restart staged)
    UPDATING = "updating"    # Windows updates installing
    RESTARTING = "restarting"
    WARNING = "warning"
    CRITICAL = "critical"
    UNREACHABLE = "unreachable"
    OFFLINE = "offline"

    @classmethod
    def values(cls) -> tuple[str, ...]:
        return tuple(s.value for s in cls)

    @classmethod
    def is_valid(cls, value: str) -> bool:
        return value in cls.values()


# Default thresholds by server type
DEFAULT_THRESHOLDS = {
    "file_server": {
        "cpu_warning": 75, "cpu_critical": 90,
        "ram_warning": 80, "ram_critical": 90,
        "disk_warning": 70, "disk_critical": 85,
    },
    "app_server": {
        "cpu_warning": 70, "cpu_critical": 85,
        "ram_warning": 75, "ram_critical": 85,
        "disk_warning": 70, "disk_critical": 85,
    },
    "domain_controller": {
        "cpu_warning": 40, "cpu_critical": 60,
        "ram_warning": 80, "ram_critical": 90,
        "disk_warning": 75, "disk_critical": 90,
    },
    "mail_server": {
        "cpu_warning": 75, "cpu_critical": 90,
        "ram_warning": 92, "ram_critical": 96,
        "disk_warning": 70, "disk_critical": 85,
    },
    "database_server": {
        "cpu_warning": 70, "cpu_critical": 85,
        "ram_warning": 70, "ram_critical": 85,
        "disk_warning": 70, "disk_critical": 85,
    },
    "web_server": {
        "cpu_warning": 70, "cpu_critical": 85,
        "ram_warning": 75, "ram_critical": 90,
        "disk_warning": 75, "disk_critical": 90,
    },
    "print_server": {
        "cpu_warning": 80, "cpu_critical": 95,
        "ram_warning": 80, "ram_critical": 90,
        "disk_warning": 75, "disk_critical": 90,
    },
    "backup_server": {
        "cpu_warning": 80, "cpu_critical": 95,
        "ram_warning": 80, "ram_critical": 90,
        "disk_warning": 70, "disk_critical": 85,
    },
    "other": {
        "cpu_warning": 75, "cpu_critical": 90,
        "ram_warning": 80, "ram_critical": 90,
        "disk_warning": 75, "disk_critical": 90,
    },
}

# Fallback for unknown server types
DEFAULT_THRESHOLDS["_default"] = DEFAULT_THRESHOLDS["file_server"]


@dataclass
class ServerConfig:
    name: str
    host: str
    username: str
    password: str
    type: str = "file_server"
    port: int = 5985
    thresholds: dict = field(default_factory=dict)
    # Tier classification for RBAC: 0 = critical (DC, mail, primary DB), 1 = standard, 2 = dev/test
    tier: int = 1
    # WinRM transport: when True, use HTTPS (5986) with cert validation
    use_https: bool = False
    # When use_https=True: skip cert validation (NOT recommended). Allows self-signed for first-rollout.
    https_skip_verify: bool = False

    def __post_init__(self):
        defaults = DEFAULT_THRESHOLDS.get(self.type, DEFAULT_THRESHOLDS["_default"])
        for key, value in defaults.items():
            self.thresholds.setdefault(key, value)
        # Auto-correct port when use_https flips and the port is the other default
        if self.use_https and self.port == 5985:
            self.port = 5986
        elif (not self.use_https) and self.port == 5986:
            self.port = 5985

    def to_dict(self) -> dict:
        """Serialize to dict with password MASKED. Never exposes real passwords."""
        return {
            "name": self.name,
            "host": self.host,
            "username": self.username,
            "password": PASSWORD_MASK,
            "type": self.type,
            "port": self.port,
            "thresholds": self.thresholds,
            "tier": self.tier,
            "use_https": self.use_https,
            "https_skip_verify": self.https_skip_verify,
        }

    def __repr__(self):
        return (
            f"ServerConfig(name={self.name!r}, host={self.host!r}, "
            f"type={self.type!r}, port={self.port}, tier={self.tier}, https={self.use_https})"
        )

    @classmethod
    def from_dict(cls, data: dict) -> "ServerConfig":
        return cls(
            name=data["name"],
            host=data["host"],
            username=data.get("username", "administrator"),
            password=data.get("password", ""),
            type=data.get("type", "file_server"),
            port=int(data.get("port", 5985)),
            thresholds=dict(data.get("thresholds", {})),
            tier=int(data.get("tier", 1)),
            use_https=bool(data.get("use_https", False)),
            https_skip_verify=bool(data.get("https_skip_verify", False)),
        )
