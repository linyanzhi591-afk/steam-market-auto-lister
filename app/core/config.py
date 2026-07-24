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

    model_config = SettingsConfigDict(env_prefix="STEAM_LISTER_", env_file=".env")


settings = Settings()
