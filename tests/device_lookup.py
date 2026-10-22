"""Use HA's replacement device lookup while testing the older supported floor."""


def device_by_identifier(registry, identifier):
    if hasattr(registry, "async_get_devices"):
        devices = registry.async_get_devices(identifiers={identifier})
        assert len(devices) <= 1
        return devices[0] if devices else None
    return registry.async_get_device(identifiers={identifier})
