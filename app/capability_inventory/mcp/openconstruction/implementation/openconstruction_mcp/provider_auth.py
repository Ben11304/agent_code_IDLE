from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any


SECURITY_NOTICE = (
    "Authenticate in a local terminal controlled by the user. Never paste access "
    "tokens, passwords, OAuth authorization codes, cookies, or credential files into chat."
)


def _auth_key(provider: str | None, method: str | None) -> str:
    method_key = (method or "").strip().casefold()
    provider_key = (provider or "").strip().casefold()
    if method_key == "huggingface_snapshot" or "huggingface" in provider_key:
        return "huggingface"
    if method_key == "github_clone" or provider_key == "github":
        return "github"
    if method_key == "kaggle_competition" or "kaggle" in provider_key:
        return "kaggle"
    if method_key == "designsafe_globus" or "globus" in provider_key or "designsafe" in provider_key:
        return "globus"
    if method_key == "google_drive_folder" or "google drive" in provider_key:
        return "google_drive"
    if method_key == "roboflow_version" or "roboflow" in provider_key:
        return "roboflow"
    if method_key == "baidu_share_transfer" or "baidu" in provider_key:
        return "baidu"
    if method_key == "dataverse_collection" or "dataverse" in provider_key:
        return "dataverse"
    return provider_key or method_key or "provider"


def _guide(key: str) -> dict[str, Any]:
    guides: dict[str, dict[str, Any]] = {
        "huggingface": {
            "provider": "Hugging Face",
            "instructions": [
                {"summary": "Open a local terminal and sign in with the official CLI.", "command": "hf auth login"},
                {"summary": "Verify that the local credential is available.", "command": "hf auth whoami"},
            ],
            "documentation_url": "https://huggingface.co/docs/huggingface_hub/guides/cli#hf-auth-login",
        },
        "github": {
            "provider": "GitHub",
            "instructions": [
                {"summary": "Open a local terminal and complete GitHub authentication.", "command": "gh auth login"},
                {"summary": "Configure Git to use the local GitHub CLI credential.", "command": "gh auth setup-git"},
                {"summary": "Verify the local login.", "command": "gh auth status"},
            ],
            "documentation_url": "https://cli.github.com/manual/gh_auth_login",
        },
        "kaggle": {
            "provider": "Kaggle",
            "instructions": [
                {"summary": "Accept the dataset or competition rules in Kaggle first, when required.", "command": None},
                {"summary": "Authenticate using the Kaggle CLI in a local terminal.", "command": "kaggle auth login"},
            ],
            "documentation_url": "https://github.com/Kaggle/kaggle-api#authentication",
        },
        "globus": {
            "provider": "Globus",
            "instructions": [
                {"summary": "Complete the Globus browser authorization from a local terminal.", "command": "globus login"},
                {"summary": "Verify the local Globus identity.", "command": "globus whoami"},
            ],
            "documentation_url": "https://docs.globus.org/cli/using-the-cli/",
        },
        "google_drive": {
            "provider": "Google Drive",
            "instructions": [
                {
                    "summary": "Complete the Google OAuth flow in the provider-specific local CLI or application named by the dataset instructions.",
                    "command": None,
                }
            ],
            "documentation_url": "https://developers.google.com/drive/api/guides/api-specific-auth",
        },
        "roboflow": {
            "provider": "Roboflow",
            "instructions": [
                {
                    "summary": "Configure the Roboflow API key in the local MCP environment, without sending it through chat.",
                    "command": None,
                }
            ],
            "documentation_url": "https://docs.roboflow.com/api-reference/authentication",
        },
        "baidu": {
            "provider": "Baidu Netdisk",
            "instructions": [
                {"summary": "Authenticate interactively in a local BaiduPCS-Go terminal session.", "command": "BaiduPCS-Go login"}
            ],
            "documentation_url": "https://github.com/qjfoidnh/BaiduPCS-Go",
        },
        "dataverse": {
            "provider": "Dataverse",
            "instructions": [
                {
                    "summary": "Configure the Dataverse API token only in the local provider client or MCP environment.",
                    "command": None,
                }
            ],
            "documentation_url": "https://guides.dataverse.org/en/latest/api/auth.html",
        },
    }
    if key in guides:
        return guides[key]
    return {
        "provider": key.replace("_", " ").title(),
        "instructions": [
            {
                "summary": "Follow the provider's local authentication flow described by the dataset access instructions.",
                "command": None,
            }
        ],
        "documentation_url": None,
    }


def _command_succeeds(command: list[str]) -> bool:
    if shutil.which(command[0]) is None:
        return False
    try:
        result = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def _credential_detected(key: str) -> bool:
    if key == "huggingface":
        try:
            from huggingface_hub import get_token

            return bool(get_token())
        except (ImportError, OSError):
            return bool(os.environ.get("HF_TOKEN"))
    if key == "github":
        return _command_succeeds(["gh", "auth", "status", "--hostname", "github.com"])
    if key == "kaggle":
        if os.environ.get("KAGGLE_API_TOKEN") or (os.environ.get("KAGGLE_USERNAME") and os.environ.get("KAGGLE_KEY")):
            return True
        return (Path.home() / ".kaggle" / "kaggle.json").is_file()
    if key == "globus":
        return _command_succeeds(["globus", "whoami"])
    if key == "roboflow":
        return bool(os.environ.get("ROBOFLOW_API_KEY"))
    if key == "dataverse":
        return bool(os.environ.get("DATAVERSE_API_TOKEN"))
    return False


def provider_auth_status(
    provider: str | None,
    method: str | None,
    *,
    required: bool,
    detect_credentials: bool = True,
) -> dict[str, Any]:
    key = _auth_key(provider, method)
    guide = _guide(key)
    detected = _credential_detected(key) if required and detect_credentials else False
    return {
        "required": required,
        "status": "ready" if not required or detected else "auth_required",
        "auth_mode": "local_provider",
        "provider": guide["provider"],
        "credential_detected": detected if required and detect_credentials else None,
        "instructions": guide["instructions"] if required else [],
        "documentation_url": guide["documentation_url"] if required else None,
        "security_notice": SECURITY_NOTICE if required else None,
    }


def auth_required_response(
    provider: str | None,
    method: str | None,
    *,
    dataset_id: str,
    destination: str | None,
    accept_license: bool,
) -> dict[str, Any]:
    auth = provider_auth_status(provider, method, required=True)
    arguments: dict[str, Any] = {"dataset_id": dataset_id, "accept_license": accept_license}
    if destination:
        arguments["destination"] = destination
    return {
        "status": "auth_required",
        "message": (
            f"Local {auth['provider']} authentication is required before OpenConstruction can download this dataset."
        ),
        "auth": auth,
        "retry": {"tool": "download_dataset", "arguments": arguments},
    }
