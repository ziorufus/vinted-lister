"""Lightweight .env loader and market settings (no external deps)."""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent


def env_file() -> Path:
    """Path of the settings/tokens file.

    $VINTED_ENV_FILE if set and non-empty (relative paths resolve against the
    current directory), else <repo>/.env. Evaluated on every call, so callers
    may set the variable after importing the package. Only read from
    os.environ, never from the file itself.
    """
    custom = os.environ.get("VINTED_ENV_FILE", "").strip()
    if custom:
        return Path(custom).expanduser()
    return ROOT / ".env"


def load_env(path: Path | str | None = None) -> dict[str, str]:
    env: dict[str, str] = {}
    p = Path(path) if path is not None else env_file()
    if p.is_dir():
        raise RuntimeError(
            f"{p} is a directory (a virtualenv?), but it must be the settings file. "
            "Move/rename it (the README uses .venv for the virtualenv) and "
            "create it from .env.example."
        )
    if not p.exists():
        raise FileNotFoundError(
            f"settings file not found: {p}. Create it with `vinted auth import` "
            "or copy .env.example."
        )
    for line in p.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    return env


def update_env(values: dict[str, str], path: Path | str | None = None) -> None:
    """Update/insert keys in the settings file, preserving lines and comments.

    Writes atomically (temp file in the same directory + fsync + os.replace).
    A symlink is followed and its target rewritten, so the link survives.
    Existing permissions are kept; a new file is created with 0o600 (it holds
    secrets). Missing parent directories are created.
    """
    p = (Path(path) if path is not None else env_file()).expanduser().resolve()
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
    _atomic_write(p, "\n".join(out) + "\n")


def _atomic_write(target: Path, text: str) -> None:
    mode = (target.stat().st_mode & 0o777) if target.exists() else 0o600
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=target.parent, prefix=f".{target.name}.", suffix=".tmp",
        delete=False,
    )
    try:
        with tmp:
            tmp.write(text)
            tmp.flush()
            os.fsync(tmp.fileno())
        os.chmod(tmp.name, mode)
        os.replace(tmp.name, target)
    except BaseException:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass
        raise


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
    attribute_aliases: dict = field(default_factory=dict)
    brand_prices: dict = field(default_factory=dict)

    @property
    def host(self) -> str:
        return f"www.vinted.{self.domain}"

    @property
    def base_url(self) -> str:
        return f"https://{self.host}/api/v2"

    @property
    def search_url(self) -> str:
        """Catalogue search service (separate api.* host, not /api/v2)."""
        return f"https://api.vinted.{self.domain}/svc-catalogue/items"

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
        f = env_file()
        env = load_env(f) if f.is_file() else {}
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
    attrs: dict[str, dict[str, int]] = {}
    for code, names in (cfg.get("attribute_aliases") or {}).items():
        if isinstance(names, str):  # file name relative to the markets file's folder
            names = json.loads(((path or MARKETS_FILE).parent / names).read_text(encoding="utf-8"))
        attrs[str(code)] = {
            str(k).strip().lower(): int(v) for k, v in (names or {}).items() if not str(k).startswith("_")
        }
    cfg["attribute_aliases"] = attrs
    return Market(domain=domain, **cfg)
