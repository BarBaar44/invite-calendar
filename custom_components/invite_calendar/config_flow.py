"""Config flow for Invite Calendar.

Steps: mailbox (validated by logging in), a store menu, then either the
.ics file or the CalDAV collection, each with the calendar name. A reauth
step asks for new passwords when the IMAP or CalDAV login starts failing.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
    OptionsFlowWithReload,
)
from homeassistant.const import (
    CONF_HOST,
    CONF_NAME,
    CONF_PASSWORD,
    CONF_PORT,
    CONF_USERNAME,
)
from homeassistant.core import callback
from homeassistant.data_entry_flow import section
from homeassistant.helpers import selector
from homeassistant.util import slugify

from .const import (
    ACCEPT_POLICIES,
    CONF_ACCEPT_POLICY,
    CONF_ATTENDEE_CN,
    CONF_CALDAV_PASSWORD,
    CONF_CALDAV_URL,
    CONF_CALDAV_USERNAME,
    CONF_FOLDER,
    CONF_FROM_NAME,
    CONF_ICS_PATH,
    CONF_MISSING_LOCATION_REPLY,
    CONF_MISSING_LOCATION_TEXT,
    CONF_PROCESSED_KEYWORD,
    CONF_RETENTION_DAYS,
    CONF_SCAN_INTERVAL_MINUTES,
    CONF_SMTP_HOST,
    CONF_SMTP_PASSWORD,
    CONF_SMTP_PORT,
    CONF_SMTP_SECTION,
    CONF_SMTP_USERNAME,
    CONF_STORE_TYPE,
    DEFAULT_FOLDER,
    DEFAULT_ICS_DIR,
    DEFAULT_IMAP_PORT,
    DEFAULT_PROCESSED_KEYWORD,
    DOMAIN,
    LOGGER,
    STORE_CALDAV,
    STORE_ICS,
)
from .mail import imap, smtp
from .options import resolve
from .store import StoreAuthError, StoreError
from .store.caldav import (
    CalDavNotCalendarError,
    CalDavSettings,
    async_validate,
    normalize_url,
)

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

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Accept policy, replies, retention, poll interval, SMTP."""
        return InviteCalendarOptionsFlow()

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
                    return await self.async_step_store()

        return self.async_show_form(
            step_id="user",
            data_schema=_mailbox_schema(user_input or {}),
            errors=errors,
        )

    async def async_step_store(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Where the calendar is stored."""
        return self.async_show_menu(
            step_id="store", menu_options=[STORE_ICS, STORE_CALDAV]
        )

    def _local_part(self) -> str:
        return self._mailbox[CONF_USERNAME].split("@", 1)[0] or "calendar"

    def _default_name(self) -> str:
        return self._local_part().replace(".", " ").replace("_", " ").title()

    async def async_step_ics(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Calendar name and .ics file."""
        errors: dict[str, str] = {}
        default_name = self._default_name()
        default_path = self.hass.config.path(
            DEFAULT_ICS_DIR, f"{slugify(self._local_part())}.ics"
        )

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

    async def async_step_caldav(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Calendar name and CalDAV collection, validated by listing events."""
        errors: dict[str, str] = {}
        if user_input is not None:
            name = user_input[CONF_NAME].strip()
            url = normalize_url(user_input[CONF_CALDAV_URL])
            settings = CalDavSettings(
                url=url,
                username=user_input[CONF_CALDAV_USERNAME].strip(),
                password=user_input[CONF_CALDAV_PASSWORD],
            )
            if not name:
                errors[CONF_NAME] = "name_required"
            elif not url.lower().startswith(("https://", "http://")):
                errors[CONF_CALDAV_URL] = "invalid_url"
            elif any(
                normalize_url(e.data.get(CONF_CALDAV_URL) or "") == url
                for e in self._async_current_entries(include_ignore=False)
            ):
                errors[CONF_CALDAV_URL] = "url_in_use"
            elif key := await self._caldav_error(settings):
                errors["base"] = key
            else:
                return self.async_create_entry(
                    title=name,
                    data={
                        **self._mailbox,
                        CONF_STORE_TYPE: STORE_CALDAV,
                        CONF_CALDAV_URL: settings.url,
                        CONF_CALDAV_USERNAME: settings.username,
                        CONF_CALDAV_PASSWORD: settings.password,
                    },
                )

        defaults = user_input or {}
        return self.async_show_form(
            step_id="caldav",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_NAME, default=defaults.get(CONF_NAME, self._default_name())
                    ): str,
                    vol.Required(
                        CONF_CALDAV_URL, default=defaults.get(CONF_CALDAV_URL, "")
                    ): str,
                    vol.Required(
                        CONF_CALDAV_USERNAME,
                        default=defaults.get(CONF_CALDAV_USERNAME, ""),
                    ): str,
                    vol.Required(CONF_CALDAV_PASSWORD): PASSWORD_SELECTOR,
                }
            ),
            errors=errors,
        )

    async def _caldav_error(self, settings: CalDavSettings) -> str | None:
        """Error key for CalDAV settings that don't work, None when fine."""
        try:
            count = await async_validate(self.hass, settings)
        except StoreAuthError:
            return "caldav_invalid_auth"
        except CalDavNotCalendarError as err:
            LOGGER.debug("CalDAV validation: %s", err)
            return "caldav_not_calendar"
        except StoreError as err:
            LOGGER.debug("CalDAV validation: %s", err)
            return "caldav_cannot_connect"
        LOGGER.debug("CalDAV %s holds %s event resources", settings.url, count)
        return None

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """The mailbox or the CalDAV server rejected a login."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for new passwords; an empty field keeps the stored one. Both
        logins are checked, since either may have triggered this."""
        entry = self._get_reauth_entry()
        is_caldav = entry.data.get(CONF_STORE_TYPE) == STORE_CALDAV
        errors: dict[str, str] = {}
        if user_input is not None:
            data = dict(entry.data)
            if user_input.get(CONF_PASSWORD):
                data[CONF_PASSWORD] = user_input[CONF_PASSWORD]
            if is_caldav and user_input.get(CONF_CALDAV_PASSWORD):
                data[CONF_CALDAV_PASSWORD] = user_input[CONF_CALDAV_PASSWORD]
            try:
                await self.hass.async_add_executor_job(imap.validate, _settings(data))
            except imap.ImapError as err:
                errors["base"] = _imap_error_key(err)
            else:
                if is_caldav:
                    key = await self._caldav_error(
                        CalDavSettings(
                            url=data[CONF_CALDAV_URL],
                            username=data[CONF_CALDAV_USERNAME],
                            password=data[CONF_CALDAV_PASSWORD],
                        )
                    )
                    if key:
                        errors["base"] = key
                if not errors:
                    return self.async_update_reload_and_abort(entry, data=data)

        schema: dict[Any, Any] = {vol.Optional(CONF_PASSWORD): PASSWORD_SELECTOR}
        if is_caldav:
            schema[vol.Optional(CONF_CALDAV_PASSWORD)] = PASSWORD_SELECTOR
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema(schema),
            description_placeholders={
                "username": entry.data[CONF_USERNAME],
                "host": entry.data[CONF_HOST],
                "caldav": entry.data.get(CONF_CALDAV_URL) or "-",
            },
            errors=errors,
        )


def _options_schema() -> vol.Schema:
    number = selector.NumberSelectorMode.BOX
    return vol.Schema(
        {
            vol.Required(CONF_ACCEPT_POLICY): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=list(ACCEPT_POLICIES),
                    translation_key="accept_policy",
                    mode=selector.SelectSelectorMode.DROPDOWN,
                )
            ),
            vol.Required(CONF_MISSING_LOCATION_REPLY): selector.BooleanSelector(),
            vol.Optional(CONF_MISSING_LOCATION_TEXT): selector.TextSelector(
                selector.TextSelectorConfig(multiline=True)
            ),
            vol.Required(CONF_RETENTION_DAYS): selector.NumberSelector(
                selector.NumberSelectorConfig(min=0, max=3650, step=1, mode=number)
            ),
            vol.Required(CONF_SCAN_INTERVAL_MINUTES): selector.NumberSelector(
                selector.NumberSelectorConfig(min=1, max=1440, step=1, mode=number)
            ),
            vol.Optional(CONF_FROM_NAME): str,
            vol.Optional(CONF_ATTENDEE_CN): str,
            vol.Required(CONF_SMTP_SECTION): section(
                vol.Schema(
                    {
                        vol.Optional(CONF_SMTP_HOST): str,
                        vol.Optional(CONF_SMTP_PORT): selector.NumberSelector(
                            selector.NumberSelectorConfig(
                                min=1, max=65535, step=1, mode=number
                            )
                        ),
                        vol.Optional(CONF_SMTP_USERNAME): str,
                        vol.Optional(CONF_SMTP_PASSWORD): PASSWORD_SELECTOR,
                    }
                ),
                {"collapsed": True},
            ),
        }
    )


def _clean(user_input: dict[str, Any], previous: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize numbers, drop empty strings, and keep a stored SMTP password
    when the field is left empty (password fields are never prefilled)."""
    out: dict[str, Any] = {}
    for key, value in user_input.items():
        if key == CONF_SMTP_SECTION:
            continue
        if isinstance(value, str):
            value = value.strip()
            if not value:
                continue
        if key in (CONF_RETENTION_DAYS, CONF_SCAN_INTERVAL_MINUTES):
            value = int(value)
        out[key] = value
    smtp_in = dict(user_input.get(CONF_SMTP_SECTION) or {})
    smtp_out: dict[str, Any] = {}
    for key, value in smtp_in.items():
        if isinstance(value, str):
            value = value.strip()
            if not value:
                continue
        if key == CONF_SMTP_PORT:
            value = int(value)
        smtp_out[key] = value
    old_smtp = previous.get(CONF_SMTP_SECTION) or {}
    if CONF_SMTP_PASSWORD not in smtp_out and old_smtp.get(CONF_SMTP_PASSWORD):
        smtp_out[CONF_SMTP_PASSWORD] = old_smtp[CONF_SMTP_PASSWORD]
    out[CONF_SMTP_SECTION] = smtp_out
    return out


class InviteCalendarOptionsFlow(OptionsFlowWithReload):
    """One form; saving reloads the entry."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show and validate the options."""
        entry = self.config_entry
        errors: dict[str, str] = {}
        if user_input is not None:
            cleaned = _clean(user_input, entry.options)
            opts = resolve(entry.data, cleaned, entry.title)
            if opts.sends_mail:
                if "@" not in opts.address:
                    errors["base"] = "username_not_address"
                else:
                    try:
                        await self.hass.async_add_executor_job(smtp.validate, opts.smtp)
                    except smtp.SmtpAuthError as err:
                        LOGGER.debug("SMTP validation: %s", err)
                        errors["base"] = "smtp_invalid_auth"
                    except smtp.SmtpError as err:
                        LOGGER.debug("SMTP validation: %s", err)
                        errors["base"] = "smtp_cannot_connect"
            if not errors:
                return self.async_create_entry(data=cleaned)

        current = resolve(entry.data, entry.options, entry.title)
        smtp_saved = dict(entry.options.get(CONF_SMTP_SECTION) or {})
        smtp_saved.pop(CONF_SMTP_PASSWORD, None)
        suggested = {
            CONF_ACCEPT_POLICY: current.accept_policy,
            CONF_MISSING_LOCATION_REPLY: current.missing_location_reply,
            CONF_MISSING_LOCATION_TEXT: current.missing_location_text,
            CONF_RETENTION_DAYS: current.retention_days,
            CONF_SCAN_INTERVAL_MINUTES: int(
                current.scan_interval.total_seconds() // 60
            ),
            CONF_FROM_NAME: current.from_name,
            CONF_ATTENDEE_CN: current.attendee_cn,
            CONF_SMTP_SECTION: smtp_saved,
        }
        if user_input is not None:
            suggested.update(
                {k: v for k, v in user_input.items() if k != CONF_SMTP_SECTION}
            )
        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(
                _options_schema(), suggested
            ),
            description_placeholders={
                "address": current.address,
                "smtp_host": entry.data[CONF_HOST],
            },
            errors=errors,
        )
