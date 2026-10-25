"""Constants for the Private Repositories integration."""

from __future__ import annotations

from datetime import timedelta
import logging
from typing import Final

DOMAIN: Final = "private_repositories"
LOGGER = logging.getLogger(__package__)

SUBENTRY_REPOSITORY: Final = "repository"

CONF_LOGIN: Final = "login"
CONF_USER_ID: Final = "user_id"
CONF_REPOSITORY: Final = "repository"
CONF_REPOSITORY_ID: Final = "repository_id"
CONF_INTEGRATION: Final = "integration"
CONF_PRERELEASES: Final = "prereleases"
CONF_REMINDER_DAYS: Final = "reminder_days"

# Unchanged answers do not count against GitHub's rate limit, so checking often
# costs little. homeassistant.update_entity checks at once.
SCAN_INTERVAL: Final = timedelta(minutes=15)

DEFAULT_REMINDER_DAYS: Final = 30
MIN_REMINDER_DAYS: Final = 1
MAX_REMINDER_DAYS: Final = 90

TOKENS_URL: Final = "https://github.com/settings/personal-access-tokens"
NEW_TOKEN_URL: Final = "https://github.com/settings/personal-access-tokens/new"
# Fine-grained personal access tokens. Classic tokens cannot be limited to
# reading some repositories, so they are refused.
TOKEN_PREFIX: Final = "github_pat_"

ISSUE_RESTART: Final = "restart_required"
ISSUE_TOKEN_EXPIRING: Final = "token_expiring"

# Sent when an install of an integration is asked for and when it ends, with its
# domain and the version installed (None until it is).
SIGNAL_INSTALL: Final = f"{DOMAIN}_install"

STORAGE_KEY: Final = DOMAIN
STORAGE_VERSION: Final = 1

# Work area for installs, inside custom_components/ so that the final rename
# never crosses a mount point. Home Assistant skips it: it has no manifest.json.
WORK_DIR: Final = f".{DOMAIN}"
