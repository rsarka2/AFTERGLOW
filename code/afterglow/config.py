"""Load afterglow.toml (shared by the pod and the laptop) or another profile such as park.toml."""
import os
import tomllib
from pathlib import Path

DEFAULT = Path(__file__).resolve().parent.parent / "afterglow.toml"


def load(path: str | os.PathLike | None = None) -> dict:
    p = Path(path or os.environ.get("AFTERGLOW_CONFIG", DEFAULT)).resolve()
    with open(p, "rb") as f:
        cfg = tomllib.load(f)
    # TOML keys are strings; turn the waypoint table into {int: (x, y)}
    cfg["waypoints"] = {int(k): tuple(v) for k, v in cfg.get("waypoints", {}).items()}
    cfg["_path"] = str(p)  # where it came from, for resolve()
    return cfg


def resolve(cfg: dict, rel: str | os.PathLike) -> Path:
    """A path named in the config (like data/trails.geojson), relative to the config file's folder."""
    return Path(cfg.get("_path", DEFAULT)).parent / rel
