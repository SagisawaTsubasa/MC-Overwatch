"""mc_overwatch 集成常量。"""

DOMAIN = "mc_overwatch"

CONF_HOST = "host"
CONF_PORT = "port"
CONF_TOKEN = "token"
CONF_SCAN_INTERVAL = "scan_interval"

DEFAULT_PORT = 8787
DEFAULT_SCAN_INTERVAL = 30
REQUEST_TIMEOUT = 10

# 实体 key（unique_id 后缀 + translation_key 对应）
KEY_ONLINE = "online"
KEY_PLAYERS = "players"
KEY_TPS = "tps"
KEY_MSPT = "mspt"
KEY_MEMORY = "memory"
KEY_CPU = "cpu"
KEY_UPTIME = "uptime"
KEY_VERDICT = "verdict"

# 扫描间隔边界（秒）：0/负数会让 DataUpdateCoordinator 无间隔连发请求
MIN_SCAN_INTERVAL = 5
MAX_SCAN_INTERVAL = 86400
