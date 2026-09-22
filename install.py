#!/usr/bin/env python3
"""
Zero-Friction Auto-Installer for Reflex Router.
Automatically detects and wires Reflex into OpenCode and Goose without manual editing.
"""
import sys
import json
import shutil
from pathlib import Path
from datetime import datetime

HOME = Path.home()
TIMESTAMP = datetime.now().strftime("%Y%m%d_%H%M%S")

def wire_goose() -> bool:
    """Installs Reflex as a custom provider in Goose."""
    goose_dir = HOME / ".config/goose/custom_providers"
    if not goose_dir.exists():
        print("[-] Goose custom_providers directory not found. Skipping Goose.")
        return False

    reflex_goose_file = goose_dir / "reflex.json"
    content = {
        "name": "reflex",
        "engine": "openai",
        "display_name": "Reflex System 1 Router (OpenCode-Go)",
        "description": "Autonomous intelligent model and effort router gateway on 127.0.0.1:8787",
        "api_key_env": "OPENCODE_GO_API_KEY",
        "base_url": "http://127.0.0.1:8787/v1",
        "models": [
            { "name": "auto", "context_limit": 128000, "input_token_cost": 0.0, "output_token_cost": 0.0, "reasoning": True }
        ],
        "supports_streaming": True,
        "requires_auth": False,
        "headers": None
    }

    try:
        with open(reflex_goose_file, "w") as f:
            json.dump(content, f, indent=2)
        print(f"[+] Successfully wired Goose! Created {reflex_goose_file}")
        return True
    except Exception as e:
        print(f"[-] Failed to wire Goose: {e}")
        return False

def wire_opencode() -> bool:
    """Adds Reflex provider to OpenCode configuration."""
    opencode_cfg = HOME / ".config/opencode/opencode.json"
    if not opencode_cfg.exists():
        print("[-] OpenCode config not found. Skipping OpenCode.")
        return False

    backup_file = opencode_cfg.parent / f"opencode.json.bak-reflex-{TIMESTAMP}"
    try:
        shutil.copy2(opencode_cfg, backup_file)
        print(f"[+] Created OpenCode config backup: {backup_file}")

        with open(opencode_cfg, "r") as f:
            data = json.load(f)

        if "provider" not in data:
            data["provider"] = {}

        data["provider"]["reflex"] = {
            "npm": "@ai-sdk/openai-compatible",
            "name": "Reflex System 1 Router",
            "options": {
                "baseURL": "http://127.0.0.1:8787/v1",
                "apiKey": "local-key"
            },
            "models": {
                "auto": {
                    "id": "auto",
                    "name": "Reflex Intelligent Auto"
                }
            }
        }

        with open(opencode_cfg, "w") as f:
            json.dump(data, f, indent=2)

        print(f"[+] Successfully wired OpenCode! Added 'reflex' provider to {opencode_cfg}")
        return True
    except Exception as e:
        print(f"[-] Failed to wire OpenCode: {e}")
        return False

def main():
    print("=== Reflex Auto-Wiring Wizard ===")
    g_ok = wire_goose()
    oc_ok = wire_opencode()
    print("=================================")
    if g_ok or oc_ok:
        print("Done! You can now use model 'reflex/auto' in OpenCode and provider 'reflex' in Goose.")
    else:
        print("No supported harnesses found to auto-wire.")

if __name__ == "__main__":
    main()
