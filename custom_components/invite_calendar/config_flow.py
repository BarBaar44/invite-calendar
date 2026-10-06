"""Config flow for Invite Calendar.

Steps: mailbox (validated by logging in), then the .ics store and the
calendar name. CalDAV joins as a second store type in milestone 2. A
reauth step asks for a new password when the IMAP login starts failing.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import (
    CONF_HOST,
    CONF_NAME,
    CONF_PASSWORD,
    CONF_PORT,
    CONF_USERNAME,
)
from homeassistant.helpers import selector
from homeassistant.util import slugify

from .const import (
    CONF_FOLDER,
    CONF_ICS_PATH,
    CONF_PROCESSED_KEYWORD,
    CONF_STORE_TYPE,
    DEFAULT_FOLDER,
    DEFAULT_ICS_DIR,
    DEFAULT_IMAP_PORT,
    DEFAULT_PROCESSED_KEYWORD,
    DOMAIN,
    LOGGER,
    STORE_ICS,
)
from .mail import imap

PASSWORD_SELECTOR = selector.TextSelector(
    selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
)


def _mailbox_schema(defaults: Mapping[str, Any]) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(CONF_HOST, default=defaults.get(CONF_HOST, "")): str,
            vol.Required(
                CONF_PORT, default=defaults.get(CONF_PORT, DEFAULT_IMAP_PORT)
            ): vol.All(vol.Coerce(int), vol.Range(min=1, max=65535)),
            vol.Required(CONF_USERNAME, default=defaults.get(CONF_USERNAME, "")): str,
            vol.Required(CONF_PASSWORD): PASSWORD_SELECTOR,
            vol.Required(
                CONF_FOLDER, default=defaults.get(CONF_FOLDER, DEFAULT_FOLDER)
            ): str,
            vol.Required(
                CONF_PROCESSED_KEYWORD,
                default=defaults.get(CONF_PROCESSED_KEYWORD, DEFAULT_PROCESSED_KEYWORD),
            ): str,
        }
    )


def _settings(data: Mapping[str, Any]) -> imap.ImapSettings:
    return imap.ImapSettings(
        host=data[CONF_HOST].strip(),
        port=int(data[CONF_PORT]),
        username=data[CONF_USERNAME].strip(),
        password=data[CONF_PASSWORD],
        folder=data[CONF_FOLDER].strip(),
        keyword=data[CONF_PROCESSED_KEYWORD].strip(),
    )


def _imap_error_key(err: imap.ImapError) -> str:
    if isinstance(err, imap.ImapAuthError):
        return "invalid_auth"
    if isinstance(err, imap.ImapFolderError):
        return "folder_not_found"
    if isinstance(err, imap.ImapKeywordError):
        return "keywords_not_supported"
    return "cannot_connect"


def _valid_keyword(keyword: str) -> bool:
    """An IMAP flag keyword is an atom: no spaces, no specials, no backslash."""
    return (
        bool(keyword)
        and not any(ch in keyword for ch in ' ()[]{}%*"\\')
        and keyword.isascii()
    )


def check_ics_path(config_dir: str, path: str) -> str | None:
    """Error key for an unusable .ics path, None when fine. BLOCKING.

    The path must be absolute, end in .ics, lie inside the config dir, and
    its directory must be creatable and writable. An existing file is
    accepted as is (migration keeps /config/www/tesla.ics)."""
    p = Path(path)
    if not p.is_absolute() or p.suffix.lower() != ".ics":
        return "invalid_path"
    try:
        resolved = p.resolve()
        if not resolved.is_relative_to(Path(config_dir).resolve()):
            return "path_outside_config"
        if resolved.exists() and not resolved.is_file():
            return "invalid_path"
        resolved.parent.mkdir(parents=True, exist_ok=True)
        if not os.access(resolved.parent, os.W_OK):
            return "path_not_writable"
        if resolved.exists() and not os.access(resolved, os.R_OK | os.W_OK):
            return "path_not_writable"
    except OSError:
        return "path_not_writable"
    return None


class InviteCalendarConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Invite Calendar."""

    VERSION = 2
    MINOR_VERSION = 1

    def __init__(self) -> None:
        """Start empty."""
        self._mailbox: dict[str, Any] = {}

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Mailbox: IMAP server, login, folder, processed keyword."""
        errors: dict[str, str] = {}
        if user_input is not None:
            settings = _settings(user_input)
            if not _valid_keyword(settings.keyword):
                errors[CONF_PROCESSED_KEYWORD] = "invalid_keyword"
            else:
                await self.async_set_unique_id(
                    f"{settings.username}@{settings.host}/{settings.folder}".lower()
                )
                self._abort_if_unique_id_configured()
                try:
                    await self.hass.async_add_executor_job(imap.validate, settings)
                except imap.ImapError as err:
                    LOGGER.debug("IMAP validation failed: %s", err)
                    errors["base"] = _imap_error_key(err)
                else:
                    self._mailbox = {
                        CONF_HOST: settings.host,
                        CONF_PORT: settings.port,
                        CONF_USERNAME: settings.username,
                        CONF_PASSWORD: settings.password,
                        CONF_FOLDER: settings.folder,
                        CONF_PROCESSED_KEYWORD: settings.keyword,
                    }
                    return await self.async_step_ics()

        return self.async_show_form(
            step_id="user",
            data_schema=_mailbox_schema(user_input or {}),
            errors=errors,
        )

    async def async_step_ics(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Calendar name and .ics file."""
        errors: dict[str, str] = {}
        local = self._mailbox[CONF_USERNAME].split("@", 1)[0] or "calendar"
        default_name = local.replace(".", " ").replace("_", " ").title()
        default_path = self.hass.config.path(DEFAULT_ICS_DIR, f"{slugify(local)}.ics")

        if user_input is not None:
            name = user_input[CONF_NAME].strip()
            path = user_input[CONF_ICS_PATH].strip()
            if not name:
                errors[CONF_NAME] = "name_required"
            elif any(
                e.data.get(CONF_ICS_PATH) == path
                for e in self._async_current_entries(include_ignore=False)
            ):
                errors[CONF_ICS_PATH] = "path_in_use"
            elif err := await self.hass.async_add_executor_job(
                check_ics_path, self.hass.config.config_dir, path
            ):
                errors[CONF_ICS_PATH] = err
            else:
                return self.async_create_entry(
                    title=name,
                    data={
                        **self._mailbox,
                        CONF_STORE_TYPE: STORE_ICS,
                        CONF_ICS_PATH: path,
                    },
                )

        defaults = user_input or {}
        return self.async_show_form(
            step_id="ics",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_NAME, default=defaults.get(CONF_NAME, default_name)
                    ): str,
                    vol.Required(
                        CONF_ICS_PATH, default=defaults.get(CONF_ICS_PATH, default_path)
                    ): str,
                }
            ),
            errors=errors,
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """IMAP login started failing."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for the new password."""
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            data = {**entry.data, CONF_PASSWORD: user_input[CONF_PASSWORD]}
            try:
                await self.hass.async_add_executor_job(imap.validate, _settings(data))
            except imap.ImapError as err:
                errors["base"] = _imap_error_key(err)
            else:
                return self.async_update_reload_and_abort(entry, data=data)

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Required(CONF_PASSWORD): PASSWORD_SELECTOR}),
            description_placeholders={
                "username": entry.data[CONF_USERNAME],
                "host": entry.data[CONF_HOST],
            },
            errors=errors,
        )
