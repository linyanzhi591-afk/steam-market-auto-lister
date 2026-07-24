from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """仅允许本机访问的默认配置。"""

    app_name: str = "Steam 饰品自动上架"
    host: str = "127.0.0.1"
    port: int = 8765
    data_dir: Path = Path("data")
    steam_profile_dir: Path = Path("data/steam-browser-profile")
    login_timeout_seconds: int = 300
    dry_run: bool = True
    allow_market_writes: bool = False
    sync_interval_seconds: int = 900
    request_delay_seconds: float = 1.5
    default_currency: str = "CNY"
    max_batch_items: int = 2000
    max_unit_buyer_price_minor: int = 100_000

    model_config = SettingsConfigDict(env_prefix="STEAM_LISTER_", env_file=".env")


settings = Settings()
