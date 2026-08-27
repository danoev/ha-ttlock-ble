"""Home Assistant-specific command outcome tracking for the pinned TTLock SDK."""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING, Self

from ttlock_ble import TTLockClient, TTLockError
from ttlock_ble import commands as cmd
from ttlock_ble.crypto import aes_decrypt

from .const import LOGGER

if TYPE_CHECKING:
    from collections.abc import Callable

    from bleak import BleakClient
    from bleak.backends.device import BLEDevice

    from ttlock_ble import VirtualKey
    from ttlock_ble.protocol import Frame


class ControlStage(StrEnum):
    """Safe, credential-free milestones for an explicit bolt command."""

    BEFORE_AUTH = "before_check_user_time"
    AUTH_WRITE_STARTED = "check_user_time_write_started"
    AUTH_SENT = "check_user_time_sent"
    AUTHENTICATED = "authenticated"
    CONTROL_WRITE_STARTED = "control_write_started"
    CONTROL_FRAME_WRITTEN = "control_frame_written"
    CONTROL_ACK_RECEIVED = "control_ack_received"
    CONTROL_ACKNOWLEDGED = "control_acknowledged"
    CONTROL_ACK_REJECTED = "control_ack_rejected"


class ControlOutcomeUnknownError(TTLockError):
    """The control frame was written but its successful outcome is unconfirmed."""

    def __init__(
        self,
        action: str,
        *,
        disconnected: bool,
        routed_echoes: tuple[int, ...],
        response_unclassified: bool,
    ) -> None:
        """Describe the ambiguity without retaining credentials or wire payloads."""
        self.action = action
        self.disconnected = disconnected
        self.routed_echoes = routed_echoes
        self.response_unclassified = response_unclassified
        detail = (
            "the BLE link dropped"
            if disconnected
            else "no matching acknowledgement arrived"
        )
        if response_unclassified:
            detail = "a response arrived but could not be classified safely"
        super().__init__(
            f"{action} command was sent, but its outcome is unknown because {detail}; "
            "the command was not retried"
        )


class ControlAbortedForShutdownError(TTLockError):
    """Physical control was safely stopped before its first control write."""

    def __init__(self, action: str, *, boundary: str) -> None:
        """Describe a credential-free, known pre-control shutdown outcome."""
        super().__init__(
            f"{action} aborted because Home Assistant is shutting down {boundary}; "
            "no control command was sent"
        )


class TtlockBleClient(TTLockClient):
    """
    TTLock client that distinguishes pre-write failure from lost acknowledgement.

    The integration pins ``ttlock-ble==0.1.11``. That SDK correctly sends only one
    control frame, but its public ``lock``/``unlock`` methods collapse every exchange
    timeout into the same ``TTLockError``. This narrow adapter preserves the SDK's
    wire behavior while exposing whether every control-frame chunk was accepted by
    the BLE stack before the acknowledgement was lost.
    """

    def __init__(
        self,
        key: VirtualKey,
        *,
        device: BLEDevice | None = None,
        disconnected_callback: Callable[[BleakClient], None] | None = None,
        keep_alive_after_command: float = 25.0,
        connect_attempts: int = 3,
    ) -> None:
        """Configure command-stage tracking around the SDK client."""
        self._ha_disconnected_callback = disconnected_callback
        self._ha_control_allowed: Callable[[], bool] | None = None
        self._ha_control_opcode: int | None = None
        self._ha_control_stage = ControlStage.BEFORE_AUTH
        self._ha_control_frame_written = False
        self._ha_control_ack_received = False
        self._ha_response_unclassified = False
        self._ha_disconnected_during_control = False
        self._ha_routed_echoes: list[int] = []
        self._ha_control_active = False
        super().__init__(
            key,
            device=device,
            disconnected_callback=self._ha_on_disconnected,
            keep_alive_after_command=keep_alive_after_command,
            connect_attempts=connect_attempts,
        )

    @classmethod
    def from_ble_device(
        cls,
        device: BLEDevice,
        key: VirtualKey,
        *,
        disconnected_callback: Callable[[BleakClient], None] | None = None,
        keep_alive_after_command: float = 25.0,
        connect_attempts: int = 3,
    ) -> Self:
        """Build around a BLE device resolved by Home Assistant."""
        return cls(
            key,
            device=device,
            disconnected_callback=disconnected_callback,
            keep_alive_after_command=keep_alive_after_command,
            connect_attempts=connect_attempts,
        )

    @property
    def control_stage(self) -> ControlStage:
        """Return the latest safe explicit-control milestone."""
        return self._ha_control_stage

    def set_control_allowed(self, control_allowed: Callable[[], bool]) -> None:
        """Install the owning integration's synchronous pre-control gate."""
        self._ha_control_allowed = control_allowed

    async def unlock(self) -> None:
        """Unlock once, retaining an ambiguous post-write outcome."""
        await self._ha_run_control(cmd.CMD_UNLOCK, "unlock")

    async def lock(self) -> None:
        """Lock once, retaining an ambiguous post-write outcome."""
        await self._ha_run_control(cmd.CMD_LOCK, "lock")

    async def _ha_run_control(self, opcode: int, action: str) -> None:
        """Run the SDK handshake and one control exchange with stage attribution."""
        self._ha_reset_control_tracking(opcode)
        self._ha_control_active = True
        LOGGER.debug(
            "Control stage for %s: %s", self.key.lockMac, self._ha_control_stage
        )
        try:
            async with self._command_lock:
                self._ha_require_control_allowed(
                    action,
                    boundary="before authentication",
                )
                try:
                    ps_from_lock = await self._check_user_time()
                except Exception:
                    LOGGER.debug(
                        "Control authentication failed for %s (stage=%s)",
                        self.key.lockMac,
                        self._ha_control_stage,
                    )
                    raise
                self._ha_control_stage = ControlStage.AUTHENTICATED
                LOGGER.debug(
                    "Control stage for %s: %s", self.key.lockMac, self._ha_control_stage
                )
                self._ha_require_control_allowed(action, boundary="before control")
                try:
                    await self._control_lock(opcode, ps_from_lock, action)
                except TTLockError as exc:
                    if self._ha_control_ack_received:
                        self._ha_control_stage = ControlStage.CONTROL_ACK_REJECTED
                        LOGGER.debug(
                            "Control acknowledgement rejected for %s "
                            "(action=%s, stage=%s)",
                            self.key.lockMac,
                            action,
                            self._ha_control_stage,
                        )
                        raise
                    if not self._ha_control_frame_written:
                        LOGGER.debug(
                            "Control failed before the complete frame was written for "
                            "%s (action=%s, stage=%s)",
                            self.key.lockMac,
                            action,
                            self._ha_control_stage,
                        )
                        raise
                    LOGGER.warning(
                        "Control outcome unknown for %s "
                        "(action=%s, stage=%s, disconnected=%s, "
                        "routed_echoes=%s, response_unclassified=%s)",
                        self.key.lockMac,
                        action,
                        self._ha_control_stage,
                        self._ha_disconnected_during_control,
                        [f"0x{echo:02x}" for echo in self._ha_routed_echoes],
                        self._ha_response_unclassified,
                    )
                    raise ControlOutcomeUnknownError(
                        action,
                        disconnected=self._ha_disconnected_during_control,
                        routed_echoes=tuple(self._ha_routed_echoes),
                        response_unclassified=self._ha_response_unclassified,
                    ) from exc
            self._ha_control_stage = ControlStage.CONTROL_ACKNOWLEDGED
            LOGGER.debug(
                "Control stage for %s: %s", self.key.lockMac, self._ha_control_stage
            )
            self._restart_keep_alive()
        finally:
            self._ha_control_active = False

    def _ha_require_control_allowed(self, action: str, *, boundary: str) -> None:
        """Abort at a known pre-control boundary when config-entry shutdown won."""
        if self._ha_control_allowed is None or self._ha_control_allowed():
            return
        LOGGER.debug(
            "Control stopped for %s before physical write "
            "(action=%s, stage=%s, boundary=%s)",
            self.key.lockMac,
            action,
            self._ha_control_stage,
            boundary,
        )
        raise ControlAbortedForShutdownError(action, boundary=boundary)

    def _ha_reset_control_tracking(self, opcode: int) -> None:
        """Reset non-secret diagnostics for one explicit operation."""
        self._ha_control_opcode = opcode
        self._ha_control_stage = ControlStage.BEFORE_AUTH
        self._ha_control_frame_written = False
        self._ha_control_ack_received = False
        self._ha_response_unclassified = False
        self._ha_disconnected_during_control = False
        self._ha_routed_echoes.clear()

    async def _send(self, frame: Frame) -> None:
        """Track complete BLE writes without logging encrypted or plaintext payloads."""
        if frame.command == cmd.CMD_CHECK_USER_TIME:
            self._ha_control_stage = ControlStage.AUTH_WRITE_STARTED
            LOGGER.debug(
                "Control stage for %s: %s", self.key.lockMac, self._ha_control_stage
            )
        elif frame.command == self._ha_control_opcode:
            self._ha_control_stage = ControlStage.CONTROL_WRITE_STARTED
            LOGGER.debug(
                "Control stage for %s: %s", self.key.lockMac, self._ha_control_stage
            )
        await super()._send(frame)
        if frame.command == cmd.CMD_CHECK_USER_TIME:
            self._ha_control_stage = ControlStage.AUTH_SENT
            LOGGER.debug(
                "Control stage for %s: %s", self.key.lockMac, self._ha_control_stage
            )
        elif frame.command == self._ha_control_opcode:
            self._ha_control_frame_written = True
            self._ha_control_stage = ControlStage.CONTROL_FRAME_WRITTEN
            LOGGER.debug(
                "Control stage for %s: %s", self.key.lockMac, self._ha_control_stage
            )

    def _answers(self, candidate: Frame, expected_command: int) -> bool:
        """Attribute safe response metadata before the SDK routes the frame."""
        answers = super()._answers(candidate, expected_command)
        if expected_command != self._ha_control_opcode:
            return answers
        try:
            plain = aes_decrypt(candidate.data, self._aes_key)
            echo, status, _data = cmd.parse_response_status(plain)
        except RuntimeError, ValueError:
            self._ha_response_unclassified = True
            LOGGER.debug(
                "Unclassified response while awaiting control acknowledgement for %s",
                self.key.lockMac,
            )
            return answers
        if echo == expected_command:
            self._ha_control_ack_received = True
            self._ha_control_stage = ControlStage.CONTROL_ACK_RECEIVED
            LOGGER.debug(
                "Control acknowledgement received for %s (echo=0x%02x, status=%d)",
                self.key.lockMac,
                echo,
                status,
            )
        else:
            self._ha_routed_echoes.append(echo)
            LOGGER.debug(
                "Response routed as push while awaiting control acknowledgement for %s "
                "(expected=0x%02x, echo=0x%02x, status=%d)",
                self.key.lockMac,
                expected_command,
                echo,
                status,
            )
        return answers

    def _ha_on_disconnected(self, client: BleakClient) -> None:
        """Record disconnect ordering and then preserve the caller's callback."""
        if self._ha_control_active:
            self._ha_disconnected_during_control = True
            LOGGER.debug(
                "BLE disconnected during explicit control for %s (stage=%s)",
                self.key.lockMac,
                self._ha_control_stage,
            )
        if self._ha_disconnected_callback is not None:
            self._ha_disconnected_callback(client)
