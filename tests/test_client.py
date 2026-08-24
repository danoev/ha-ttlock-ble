"""Regression tests for explicit-control outcome attribution."""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from ttlock_ble import TTLockError
from ttlock_ble import commands as cmd
from ttlock_ble.crypto import aes_encrypt
from ttlock_ble.protocol import Frame

from custom_components.ttlock_ble.client import (
    ControlOutcomeUnknownError,
    ControlStage,
    TtlockBleClient,
)


def _client(sample_virtual_key) -> TtlockBleClient:
    return TtlockBleClient(sample_virtual_key, device=MagicMock())


async def test_authentication_timeout_is_a_known_pre_command_failure(
    sample_virtual_key,
) -> None:
    """A CHECK_USER_TIME failure cannot be mislabeled as a sent command."""
    client = _client(sample_virtual_key)
    client._check_user_time = AsyncMock(side_effect=TTLockError("auth timeout"))
    client._control_lock = AsyncMock()

    with pytest.raises(TTLockError, match="auth timeout") as error:
        await client.unlock()

    assert not isinstance(error.value, ControlOutcomeUnknownError)
    client._control_lock.assert_not_awaited()


async def test_timeout_before_control_write_remains_a_known_failure(
    sample_virtual_key,
) -> None:
    """A pre-write failure is safe to report as an ordinary command failure."""
    client = _client(sample_virtual_key)
    client._check_user_time = AsyncMock(return_value=123)
    client._control_lock = AsyncMock(side_effect=TTLockError("write failed"))

    with pytest.raises(TTLockError, match="write failed") as error:
        await client.lock()

    assert not isinstance(error.value, ControlOutcomeUnknownError)
    client._control_lock.assert_awaited_once()


async def test_written_control_without_ack_is_unknown_and_never_retried(
    sample_virtual_key,
) -> None:
    """A lost acknowledgement preserves ambiguity after exactly one send."""
    client = _client(sample_virtual_key)
    client._check_user_time = AsyncMock(return_value=123)

    async def _control_once(*_args) -> None:
        client._ha_control_frame_written = True
        client._ha_control_stage = ControlStage.CONTROL_FRAME_WRITTEN
        message = "Timed out waiting 6.0s for the lock to reply"
        raise TTLockError(message)

    client._control_lock = AsyncMock(side_effect=_control_once)

    with pytest.raises(ControlOutcomeUnknownError, match="was not retried"):
        await client.unlock()

    client._control_lock.assert_awaited_once()


async def test_rejected_acknowledgement_remains_a_known_failure(
    sample_virtual_key,
) -> None:
    """A received negative response is not ambiguous."""
    client = _client(sample_virtual_key)
    client._check_user_time = AsyncMock(return_value=123)

    async def _rejected(*_args) -> None:
        client._ha_control_frame_written = True
        client._ha_control_ack_received = True
        message = "lock rejected"
        raise TTLockError(message)

    client._control_lock = AsyncMock(side_effect=_rejected)

    with pytest.raises(TTLockError, match="lock rejected") as error:
        await client.lock()

    assert not isinstance(error.value, ControlOutcomeUnknownError)
    assert client.control_stage is ControlStage.CONTROL_ACK_REJECTED


def test_valid_command_response_cannot_be_swallowed_as_push(
    sample_virtual_key,
) -> None:
    """An encrypted control echo satisfies the current control exchange."""
    client = _client(sample_virtual_key)
    client._ha_reset_control_tracking(cmd.CMD_UNLOCK)
    candidate = Frame.for_lock(
        sample_virtual_key.lockVersion,
        cmd.CMD_RESPONSE,
        aes_encrypt(bytes([cmd.CMD_UNLOCK, cmd.RESPONSE_SUCCESS]), client._aes_key),
    )

    assert client._answers(candidate, cmd.CMD_UNLOCK) is True
    assert client._ha_control_ack_received is True
    assert client.control_stage is ControlStage.CONTROL_ACK_RECEIVED
    assert client._ha_routed_echoes == []


def test_push_response_is_routed_while_waiting_for_control_ack(
    sample_virtual_key,
) -> None:
    """A short-heartbeat push cannot masquerade as the control reply."""
    client = _client(sample_virtual_key)
    client._ha_reset_control_tracking(cmd.CMD_LOCK)
    candidate = Frame.for_lock(
        sample_virtual_key.lockVersion,
        cmd.CMD_RESPONSE,
        aes_encrypt(
            bytes([cmd.CMD_QUERY_STATE, cmd.RESPONSE_SUCCESS]), client._aes_key
        ),
    )

    assert client._answers(candidate, cmd.CMD_LOCK) is False
    assert client._ha_control_ack_received is False
    assert client._ha_routed_echoes == [cmd.CMD_QUERY_STATE]


async def test_send_milestones_require_complete_ble_writes(
    sample_virtual_key,
) -> None:
    """Sent stages are reached only after the SDK write returns successfully."""
    client = _client(sample_virtual_key)
    client._ha_reset_control_tracking(cmd.CMD_UNLOCK)
    auth_frame = Frame.for_lock(
        sample_virtual_key.lockVersion,
        cmd.CMD_CHECK_USER_TIME,
        b"encrypted-placeholder",
    )
    control_frame = Frame.for_lock(
        sample_virtual_key.lockVersion,
        cmd.CMD_UNLOCK,
        b"encrypted-placeholder",
    )
    sdk_send = AsyncMock()
    with patch("ttlock_ble.client.TTLockClient._send", new=sdk_send):
        await client._send(auth_frame)
        assert client.control_stage is ControlStage.AUTH_SENT
        await client._send(control_frame)

    assert client.control_stage is ControlStage.CONTROL_FRAME_WRITTEN
    assert client._ha_control_frame_written is True
    assert sdk_send.await_count == 2


async def test_failed_auth_write_never_reaches_auth_sent(
    sample_virtual_key,
) -> None:
    """A partial CHECK_USER_TIME write is distinguished from a sent frame."""
    client = _client(sample_virtual_key)
    auth_frame = Frame.for_lock(
        sample_virtual_key.lockVersion,
        cmd.CMD_CHECK_USER_TIME,
        b"encrypted-placeholder",
    )
    sdk_send = AsyncMock(side_effect=OSError("adapter dropped"))
    with (
        patch("ttlock_ble.client.TTLockClient._send", new=sdk_send),
        pytest.raises(OSError, match="adapter dropped"),
    ):
        await client._send(auth_frame)

    assert client.control_stage is ControlStage.AUTH_WRITE_STARTED


def test_disconnect_callback_records_control_stage_before_forwarding(
    sample_virtual_key,
) -> None:
    """Disconnect attribution is set before the integration callback runs."""
    observations: list[bool] = []
    client = TtlockBleClient(
        sample_virtual_key,
        device=MagicMock(),
        disconnected_callback=lambda _client: observations.append(
            client._ha_disconnected_during_control
        ),
    )
    client._ha_control_active = True
    client._ha_control_stage = ControlStage.CONTROL_FRAME_WRITTEN

    client._ha_on_disconnected(MagicMock())

    assert observations == [True]


async def test_control_diagnostics_never_log_credentials(
    sample_virtual_key,
    caplog,
) -> None:
    """Milestone diagnostics contain no key, PIN, or decrypted wire data."""
    client = _client(sample_virtual_key)
    client._check_user_time = AsyncMock(return_value=123)

    async def _ambiguous(*_args) -> None:
        client._ha_control_frame_written = True
        message = "reply lost"
        raise TTLockError(message)

    client._control_lock = AsyncMock(side_effect=_ambiguous)
    with (
        caplog.at_level(logging.DEBUG, logger="custom_components.ttlock_ble"),
        pytest.raises(ControlOutcomeUnknownError),
    ):
        await client.unlock()

    for credential in (
        sample_virtual_key.aesKeyStr,
        sample_virtual_key.unlockKey,
        sample_virtual_key.adminPs,
    ):
        assert credential not in caplog.text
