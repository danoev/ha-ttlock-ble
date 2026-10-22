"""Admin metadata permission boundary and private-reference groundwork."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
import voluptuous as vol
from homeassistant.exceptions import Unauthorized

from custom_components.ttlock_ble.credential_references import (
    ProtectedCredentialReferences,
)
from tests.test_services import _device_id


@pytest.mark.parametrize("admin", [True, False])
async def test_access_metadata_requires_admin_and_never_starts_ble(
    hass, setup_integration, mock_ttlock_connection, admin
) -> None:
    handler, schema = hass.data["websocket_api"]["ttlock_ble/access_capabilities"]
    connection = MagicMock()
    connection.user.is_admin = admin
    msg = {
        "id": 1,
        "type": "ttlock_ble/access_capabilities",
        "device_id": _device_id(hass),
    }
    if admin:
        handler(hass, connection, msg)
        response = connection.send_result.call_args.args[1]
        assert response["secret_entry"] == "not_implemented"
        assert response["identity_mapping"] == "not_established"
        assert (
            response["methods"]["fingerprint"]["s31_read_evidence"]
            == "count_corroborated"
        )
    else:
        with pytest.raises(Unauthorized):
            handler(hass, connection, msg)
        connection.send_result.assert_not_called()
    with pytest.raises(vol.Invalid):
        schema({**msg, "pin": "synthetic-secret"})
    mock_ttlock_connection.async_get_access_inventory.assert_not_awaited()


async def test_private_reference_storage_keeps_no_raw_identifier_or_identity(
    hass,
) -> None:
    refs = ProtectedCredentialReferences(hass, "synthetic-entry")
    refs._store = MagicMock()
    refs._store.async_load = AsyncMock(return_value=None)
    refs._store.async_save = AsyncMock()
    refs._store.async_remove = AsyncMock()
    await refs.async_load()
    record = refs.from_verified_record(
        "lock-a", "ic_card", "epoch-1", b"synthetic-card-uid"
    )
    assert not record.identity_verified
    assert "synthetic-card-uid" not in repr(record) + repr(
        refs._store.async_save.call_args
    )
    stored = refs._store.async_save.call_args.args[0]
    assert set(stored) == {"key"}
    refs2 = ProtectedCredentialReferences(hass, "synthetic-entry")
    refs2._store = MagicMock()
    refs2._store.async_load = AsyncMock(return_value=stored)
    await refs2.async_load()
    assert record == refs2.from_verified_record(
        "lock-a", "ic_card", "epoch-1", b"synthetic-card-uid"
    )
    assert record != refs2.from_verified_record(
        "lock-b", "ic_card", "epoch-1", b"synthetic-card-uid"
    )
    assert record != refs2.from_verified_record(
        "lock-a", "ic_card", "epoch-2", b"synthetic-card-uid"
    )
    assert record != refs2.from_verified_record(
        "lock-a", "fingerprint", "epoch-1", b"synthetic-card-uid"
    )
    await refs.async_remove()
    with pytest.raises(ValueError, match="cannot be derived"):
        refs.from_verified_record("lock-a", "ic_card", "epoch-1", b"synthetic-card-uid")
    refs._store.async_remove.assert_awaited_once()


@pytest.mark.parametrize("data", [{"key": "private-invalid"}, {"key": "00"}, {}])
async def test_bad_private_key_store_fails_closed_without_echo(hass, data) -> None:
    refs = ProtectedCredentialReferences(hass, "synthetic-entry")
    refs._store = MagicMock()
    refs._store.async_load = AsyncMock(return_value=data)
    with pytest.raises(ValueError, match="storage is invalid") as error:
        await refs.async_load()
    assert "private-invalid" not in str(error.value)


async def test_concurrent_reference_load_is_idempotent_and_failed_save_is_closed(
    hass,
) -> None:
    refs = ProtectedCredentialReferences(hass, "synthetic-entry")
    refs._store = MagicMock()
    refs._store.async_load = AsyncMock(return_value=None)
    refs._store.async_save = AsyncMock(side_effect=OSError("synthetic failure"))
    with pytest.raises(OSError, match="synthetic failure"):
        await refs.async_load()
    with pytest.raises(ValueError, match="cannot be derived"):
        refs.from_verified_record("lock", "ic_card", "epoch", b"synthetic")
    refs._store.async_save.reset_mock(side_effect=True)
    await asyncio.gather(refs.async_load(), refs.async_load())
    refs._store.async_save.assert_awaited_once()


async def test_reference_store_uses_private_atomic_file(hass, tmp_path) -> None:
    from pathlib import Path
    from stat import S_IMODE

    hass.config.config_dir = str(tmp_path)
    refs = ProtectedCredentialReferences(hass, "synthetic-private-store")
    await refs.async_load()
    # The HA fixture mocks saves; exercise the configured real writer too.
    await hass.async_add_executor_job(
        refs._store._write_prepared_data, "w", '{"data":"synthetic"}'
    )
    path = Path(refs._store.path)
    assert S_IMODE((await hass.async_add_executor_job(path.stat)).st_mode) == 0o600
    assert "synthetic-card-uid" not in await hass.async_add_executor_job(path.read_text)
    await hass.async_add_executor_job(path.unlink)
    assert not await hass.async_add_executor_job(path.exists)
