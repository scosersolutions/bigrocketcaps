"""Configuración global. Falla al arrancar si algo no cuadra."""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Self

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Env(StrEnum):
    DEV = "dev"
    PAPER = "paper"
    LIVE = "live"


class RiskLimits(BaseSettings):
    """Límites de riesgo. Cambiar cualquiera invalida la validación previa."""

    model_config = SettingsConfigDict(env_prefix="MR_", env_file=".env", extra="ignore")

    # 0,6 % x 3 posiciones = 1,8 %, por debajo del 2 % diario, con 0,2 % de colchón
    # para slippage y gaps. El 0,75 % del diseño inicial era incoherente: ver C5
    # en 03-REVISION-DISENO.md.
    risk_per_trade: float = Field(default=0.006, gt=0, le=0.02)
    max_daily_loss: float = Field(default=0.02, gt=0, le=0.05)
    max_drawdown: float = Field(default=0.06, gt=0, le=0.20)
    max_positions: int = Field(default=3, ge=1, le=10)
    max_correlation: float = Field(default=0.70, gt=0, le=1.0)
    min_rr_crypto: float = Field(default=2.5, ge=1.0)
    min_rr_stocks: float = Field(default=2.0, ge=1.0)
    max_cost_ratio: float = Field(default=0.15, gt=0, le=0.50)
    signal_ttl_minutes: int = Field(default=60, ge=1, le=1440)

    @model_validator(mode="after")
    def _coherencia(self) -> Self:
        # Con N posiciones abiertas al máximo riesgo no se puede exceder la pérdida diaria.
        exposicion_max = self.risk_per_trade * self.max_positions
        if exposicion_max > self.max_daily_loss:
            raise ValueError(
                f"riesgo incoherente: {self.max_positions} posiciones x {self.risk_per_trade:.4f} "
                f"= {exposicion_max:.4f} supera max_daily_loss={self.max_daily_loss:.4f}"
            )
        if self.max_daily_loss >= self.max_drawdown:
            raise ValueError(
                f"max_daily_loss ({self.max_daily_loss}) debe ser menor que "
                f"max_drawdown ({self.max_drawdown})"
            )
        return self


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="MR_", env_file=".env", extra="ignore", case_sensitive=False
    )

    env: Env = Env.DEV
    data_dir: Path = Path("./data")
    log_dir: Path = Path("./logs")
    log_level: str = "INFO"

    kraken_api_key: SecretStr | None = None
    kraken_api_secret: SecretStr | None = None
    kraken_futures_api_key: SecretStr | None = None
    kraken_futures_api_secret: SecretStr | None = None
    kraken_futures_demo: bool = True

    finnhub_api_key: SecretStr | None = None
    polygon_api_key: SecretStr | None = None
    marketaux_api_key: SecretStr | None = None
    sec_user_agent: str | None = None

    gemini_api_key: SecretStr | None = None

    telegram_bot_token: SecretStr | None = None
    telegram_chat_id: str | None = None

    binance_testnet_key: SecretStr | None = None
    binance_testnet_secret: SecretStr | None = None

    smtp_servidor: str | None = None
    smtp_puerto: int = 587
    smtp_usuario: str | None = None
    smtp_contrasena: SecretStr | None = None
    correo_destino: str | None = None

    @model_validator(mode="after")
    def _requisitos_por_entorno(self) -> Self:
        if self.env is Env.DEV:
            return self

        faltan = [
            nombre
            for nombre, valor in (
                ("MR_KRAKEN_FUTURES_API_KEY", self.kraken_futures_api_key),
                ("MR_KRAKEN_FUTURES_API_SECRET", self.kraken_futures_api_secret),
            )
            if valor is None
        ]
        if faltan:
            raise ValueError(f"env={self.env} exige: {', '.join(faltan)}")

        # Salvaguarda: en live, el demo de futuros debe estar desactivado y viceversa.
        if self.env is Env.LIVE and self.kraken_futures_demo:
            raise ValueError("env=live con MR_KRAKEN_FUTURES_DEMO=true: configuración contradictoria")
        if self.env is Env.PAPER and not self.kraken_futures_demo:
            raise ValueError("env=paper exige MR_KRAKEN_FUTURES_DEMO=true")
        return self

    @property
    def risk(self) -> RiskLimits:
        return RiskLimits()

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.log_dir.mkdir(parents=True, exist_ok=True)


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings


def reset_settings_cache() -> None:
    """Solo para tests."""
    global _settings
    _settings = None
