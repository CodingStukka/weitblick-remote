"""The data layer: saved connections, their passwords, imports and the app's preferences.

    from weitblick.store import ConnectionLibrary, Connection
    library = ConnectionLibrary()     # ~/.config/weitblick-remote/connections.json + Secret Service
    library.load()

Port of the macOS app's ConnectionStore package and its app-side helpers.
"""

from .connection import (AdvancedSettings, AgentAccess, AudioPlaybackMode, Connection, ConnectionProtocol,
                         DisplaySettings, DriveMapping, ImportSource, KeyboardSettings, RedirectionSettings,
                         SecuritySettings, TextInput, datetime_to_reference, format_layout_id, new_id,
                         normalized_fingerprint, now_reference, parse_layout_id, reference_to_datetime)
from .credentials import (CredentialStore, CredentialStoreError, InMemoryCredentialStore,
                          SecretServiceCredentialStore)
from .draft import KEEP_PASSWORD, REMOVE_PASSWORD, ConnectionDraft, PasswordChange
from .errors import (ConnectionStoreError, DuplicateID, InvalidConnection, InvalidStore, MissingConnection,
                     StaleImportPlan, UnsupportedSchema)
from .import_report import (ConnectionImportReport, ImportAction, ImportEntry, ImportFieldIssue, ImportPlan,
                            ImportReport)
from .library import ConnectionLibrary
from .settings import AppSettings
from .store import SORT_NAME, SORT_RECENT, ConnectionStore

__all__ = [
    "AdvancedSettings", "AgentAccess", "AppSettings", "AudioPlaybackMode", "Connection", "ConnectionDraft",
    "ConnectionImportReport", "ConnectionLibrary", "ConnectionProtocol", "ConnectionStore",
    "ConnectionStoreError", "CredentialStore", "CredentialStoreError", "DisplaySettings", "DriveMapping",
    "DuplicateID", "ImportAction", "ImportEntry", "ImportFieldIssue", "ImportPlan", "ImportReport",
    "ImportSource", "InMemoryCredentialStore", "InvalidConnection", "InvalidStore", "KEEP_PASSWORD",
    "KeyboardSettings", "MissingConnection", "PasswordChange", "REMOVE_PASSWORD", "RedirectionSettings",
    "SORT_NAME", "SORT_RECENT", "SecretServiceCredentialStore", "SecuritySettings", "StaleImportPlan",
    "TextInput", "UnsupportedSchema", "datetime_to_reference", "format_layout_id", "new_id",
    "normalized_fingerprint", "now_reference", "parse_layout_id", "reference_to_datetime",
]
