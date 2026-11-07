"""Lightweight .env loader and market settings (no external deps)."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent


def load_env(path: Path | None = None) -> dict[str, str]:
    env: dict[str, str] = {}
    p = path or (ROOT / ".env")
    if p.is_dir():
        raise RuntimeError(
            f"{p} is a directory (a virtualenv?), but it must be the settings file. "
            "Move/rename it (the README uses .venv for the virtualenv) and "
            "create .env from .env.example."
        )
    for line in p.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    return env


def update_env(values: dict[str, str], path: Path | None = None) -> None:
    """Update/insert keys in .env, preserving existing lines and comments."""
    p = path or (ROOT / ".env")
    lines = p.read_text(encoding="utf-8").splitlines() if p.exists() else []
    seen: set[str] = set()
    out: list[str] = []
    for line in lines:
        m = re.match(r"^(\w+)=", line)
        if m and m.group(1) in values:
            out.append(f"{m.group(1)}={values[m.group(1)]}")
            seen.add(m.group(1))
        else:
            out.append(line)
    for k, v in values.items():
        if k not in seen:
            out.append(f"{k}={v}")
    p.write_text("\n".join(out) + "\n", encoding="utf-8")


# --------------------------------------------------------------------------- markets
MARKETS_FILE = ROOT / "config" / "markets.json"

# .env key -> (market key, type). Lets a user tweak one setting without editing JSON.
_ENV_OVERRIDES = {
    "VINTED_CURRENCY": ("currency", str),
    "VINTED_LOCALE": ("locale", str),
    "VINTED_ACCEPT_LANGUAGE": ("accept_language", str),
    "VINTED_PRICE_FORMAT": ("price_format", str),
    "VINTED_PREFERRED_CATALOG_ROOT": ("preferred_catalog_root", str),
    "VINTED_SIZE_GROUP": ("default_size_group", int),
}


@dataclass(frozen=True)
class Market:
    """Market-specific settings (currency, locale, local labels, price rules)."""

    domain: str
    currency: str
    locale: str
    accept_language: str
    price_format: str = "{amount}"
    preferred_catalog_root: Optional[str] = None
    default_size_group: int = 14
    condition_aliases: dict = field(default_factory=dict)
    material_aliases: dict = field(default_factory=dict)
    catalog_aliases: dict = field(default_factory=dict)
    brand_prices: dict = field(default_factory=dict)

    @property
    def host(self) -> str:
        return f"www.vinted.{self.domain}"

    @property
    def base_url(self) -> str:
        return f"https://{self.host}/api/v2"

    def item_url(self, item_id, edit: bool = False) -> str:
        return f"https://{self.host}/items/{item_id}" + ("/edit" if edit else "")

    def format_price(self, amount) -> str:
        try:
            f = float(amount)
            amount = int(f) if f.is_integer() else f"{f:.2f}"
        except (TypeError, ValueError):
            pass
        return self.price_format.format(amount=amount)


def load_markets(path: Path | None = None) -> dict[str, dict]:
    p = path or MARKETS_FILE
    data = json.loads(p.read_text(encoding="utf-8"))
    return {k: v for k, v in data.items() if not k.startswith("_")}


def load_market(env: dict[str, str] | None = None, path: Path | None = None) -> Market:
    """Build the Market for VINTED_DOMAIN (default 'se'), applying .env overrides."""
    if env is None:
        env = load_env() if (ROOT / ".env").is_file() else {}
    domain = (env.get("VINTED_DOMAIN") or "se").strip().lower()
    markets = load_markets(path)
    if domain not in markets:
        raise ValueError(
            f"no market '{domain}' in {path or MARKETS_FILE} "
            f"(known: {', '.join(sorted(markets))}). Add an entry for it."
        )
    cfg = dict(markets[domain])
    for env_key, (key, cast) in _ENV_OVERRIDES.items():
        raw = env.get(env_key)
        if raw:
            cfg[key] = cast(raw)
    known = {f.name for f in fields(Market)}
    unknown = set(cfg) - known
    if unknown:
        raise ValueError(f"unknown key(s) for market '{domain}': {', '.join(sorted(unknown))}")
    for key in ("condition_aliases", "material_aliases", "catalog_aliases"):
        cfg[key] = {str(k).lower(): int(v) for k, v in (cfg.get(key) or {}).items()}
    return Market(domain=domain, **cfg)
