""".env cutover — swap OLD tokens for NEW with backup."""

import os
import shutil
from datetime import datetime
from pathlib import Path
from typing import Optional

from .config import MigrateConfig


def cutover_env(cfg: MigrateConfig, apply: bool = False) -> dict:
    """Swap NEW_* env vars into target positions. Returns diff preview.

    Steps:
      1. Read .env
      2. For each bot: find NEW_<token_env>=xxx, replace <token_env>=old with new value
      3. Remove NEW_* lines
      4. Write result (or show dry-run diff)
    """
    env_path = Path(cfg.env_file)
    if not env_path.exists():
        raise FileNotFoundError(f"Env file not found: {env_path}")

    with open(env_path) as f:
        lines = f.readlines()

    # Parse all lines into key=value pairs (preserving order)
    env_dict = {}
    for line in lines:
        stripped = line.strip()
        if "=" in stripped and not stripped.startswith("#"):
            key = stripped.split("=", 1)[0]
            env_dict[key] = stripped.split("=", 1)[1]

    # Build swap map from config
    swap_map = {}  # target_env → new_value
    remove_keys = set()

    for bot in cfg.bots:
        # Convention: NEW_<target> contains the new value
        old_key = bot.old_token_env
        new_key = bot.new_token_env

        # Check if there's a NEW_ prefixed version
        new_prefixed = f"NEW_{new_key}"
        new_val = env_dict.get(new_prefixed) or os.getenv(new_prefixed, "")

        if new_val:
            swap_map[new_key] = new_val
            remove_keys.add(new_prefixed)

            # If old token env is different from new, also set up OLD_ preservation
            if old_key != new_key:
                current_val = env_dict.get(new_key, "")
                if current_val:
                    swap_map[f"OLD_{new_key}"] = current_val

    # Build diff
    diff = {"swaps": [], "removes": [], "backup": ""}

    for target, new_val in swap_map.items():
        old_val = env_dict.get(target, "(not set)")
        diff["swaps"].append({
            "key": target,
            "old": old_val[:30] + "..." if len(old_val) > 30 else old_val,
            "new": new_val[:30] + "..." if len(new_val) > 30 else new_val,
        })

    for key in remove_keys:
        diff["removes"].append(key)

    if not apply:
        return diff

    # Backup
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = f"{env_path}.backup_{ts}"
    shutil.copy2(env_path, backup_path)
    diff["backup"] = backup_path

    # Apply swaps
    result = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            result.append(line)
            continue

        key = stripped.split("=", 1)[0] if "=" in stripped else ""

        # Skip removed keys
        if key in remove_keys:
            continue

        # Apply swap
        if key in swap_map:
            result.append(f"{key}={swap_map[key]}\n")
        else:
            result.append(line)

    # Add OLD_ keys that weren't already in the file
    existing_keys = {l.strip().split("=", 1)[0] for l in result if "=" in l.strip()}
    for key, val in swap_map.items():
        if key.startswith("OLD_") and key not in existing_keys:
            result.append(f"{key}={val}\n")

    with open(env_path, "w") as f:
        f.writelines(result)

    return diff
