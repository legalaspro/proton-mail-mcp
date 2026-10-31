"""An in-memory Sieve apply backend for offline tests; it never opens a connection."""


class FakeSieveBackend:
    name = "fake"

    def __init__(self, *, names=(), available=True, failure=None, during=None):
        self.names = set(names)
        self.available = available
        self.failure = failure
        # Runs inside create_filter before anything is recorded, like a slow server.
        self.during = during
        self.created = []
        self.calls = []

    def status(self):
        return {"available": self.available, "reason": None if self.available else "offline fake"}

    def filter_names(self, *, timeout):
        assert timeout > 0
        self.calls.append("filter_names")
        return set(self.names)

    def create_filter(self, name, script, *, timeout):
        assert timeout > 0
        self.calls.append("create_filter")
        if self.during is not None:
            self.during()
        if self.failure is not None:
            raise self.failure
        self.created.append((name, script))
        self.names.add(name)
        return f"filter-{len(self.created)}"
