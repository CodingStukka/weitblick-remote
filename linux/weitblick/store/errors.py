"""Errors of the connection store (port of ConnectionStoreError). They compare by value, like the
Swift enum cases, so tests can say `assertRaises` and check the payload."""


class ConnectionStoreError(Exception):
    def __eq__(self, other: object) -> bool:
        return type(self) is type(other) and self.args == other.args  # type: ignore[union-attr]

    def __hash__(self) -> int:
        return hash((type(self), self.args))


class UnsupportedSchema(ConnectionStoreError):
    def __init__(self, version: int):
        super().__init__(version)
        self.version = version


class InvalidConnection(ConnectionStoreError):
    def __init__(self, connection_id: str):
        super().__init__(connection_id)
        self.connection_id = connection_id


class DuplicateID(ConnectionStoreError):
    def __init__(self, connection_id: str):
        super().__init__(connection_id)
        self.connection_id = connection_id


class MissingConnection(ConnectionStoreError):
    def __init__(self, connection_id: str):
        super().__init__(connection_id)
        self.connection_id = connection_id


class StaleImportPlan(ConnectionStoreError):
    """The connections changed since the import plan was made; make a new plan."""


class InvalidStore(ConnectionStoreError):
    """The file isn't a connections file (wrong structure or types)."""
