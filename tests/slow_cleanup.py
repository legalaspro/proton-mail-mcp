"""Synthetic latency and concurrent-arrival hooks over real local IMAP sockets."""

from bridge_costs import DEFAULT_COSTS, CommandClock, WorkCounters
from bridge_sim import BridgeSim, Msg, hdr, make_client


class SlowCleanupPeer(BridgeSim):
    """Charge commands and metadata with the shared model; add slow mutations.

    All values are deterministic test calibration, never a live benchmark. The
    inherited peer uses socketpair and the interpreter's real IMAP4.uid method.
    ``arrival_hook`` can append/move fixture messages during any command.
    """

    def __init__(self, *, command_seconds=0.001, mutation_seconds=1.5, costs=DEFAULT_COSTS):
        clock = CommandClock()
        super().__init__(
            clock=clock,
            command_latency=command_seconds,
            work=WorkCounters(clock, costs),
            folders=("Folders/Reviewed",),
            labels=(),
        )
        self.mutation_seconds = mutation_seconds
        self.bulk_row_seconds = 0.0
        self.inventory_seconds = 0.0
        self.inventory_commands = 0
        self.arrival_hook = None
        self.hooks.append(self._slow_command)

    def _slow_command(self, session, folder, operation, command):
        if operation == "FETCH" and "ENVELOPE" in command:
            rows = len(self._uids(command.split()[2], list(self.uidmap[folder])))
            # An injectable slow inventory path makes fixed mailbox work visible
            # when the next approved chunk shrinks; final single-UID checks stay
            # priced by the shared metadata model.
            if rows > 50:
                self.clock.elapsed += rows * self.bulk_row_seconds
                self.clock.elapsed += self.inventory_seconds
                self.inventory_commands += 1
        if operation in {"MOVE", "COPY", "STORE"}:
            self.clock.elapsed += self.mutation_seconds
        if self.arrival_hook is not None:
            return self.arrival_hook(session, folder, operation, command)
        return None

    def populate(self, count):
        return self.add_many(
            Msg(
                hdr(
                    f"<approved-{index:03d}@example.invalid>",
                    from_="notifications@example.invalid",
                    date="Tue, 15 Sep 2026 09:00:00 +0000",
                )
            )
            for index in range(count)
        )

    def client(self, directory, *, timeout=80):
        return make_client(self, directory, call_timeout=timeout, timeout=5)
