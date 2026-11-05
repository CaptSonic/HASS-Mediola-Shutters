"""Isolated regression tests; no Home Assistant runtime or gateway required.

Execute selected production definitions with explicit API doubles. These tests
cover our compatibility wiring, not Home Assistant's own runtime behavior.
Run with: python -B -m unittest discover -s tests -v
"""
import ast
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock


ROOT = Path(__file__).resolve().parents[1] / "custom_components" / "mediola_shutters"


def load_definition(filename, name, namespace, *, parent=None):
    """Compile a real production definition without importing HA dependencies."""
    tree = ast.parse((ROOT / filename).read_text(encoding="utf-8-sig"))
    nodes = tree.body
    if parent is not None:
        nodes = next(node for node in nodes if getattr(node, "name", None) == parent).body
    node = next(node for node in nodes if getattr(node, "name", None) == name)
    # Function annotations are irrelevant to these isolated behavioral tests.
    module = ast.Module(
        body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), node],
        type_ignores=[],
    )
    ast.fix_missing_locations(module)
    exec(compile(module, str(ROOT / filename), "exec"), namespace)
    return namespace[name]


class OptionsFlowDouble:
    """Model HA's read-only config_entry, available only after initialization."""

    @property
    def config_entry(self):
        return self.hass.config_entries.async_get_known_entry(self.handler)

    def async_show_form(self, **kwargs):
        return kwargs

    def async_create_entry(self, **kwargs):
        return kwargs


class CoordinatorDouble:
    def __init__(self, hass, logger, *, config_entry, name, update_interval):
        self.hass = hass
        self.config_entry = config_entry
        self.update_interval = update_interval

    async def async_config_entry_first_refresh(self):
        self.data = await self._async_update_data()


@dataclass(kw_only=True)
class DeviceInfoDouble:
    identifiers: set
    name: str
    manufacturer: str
    model: str
    via_device_id: str | None


class CompatibilityTests(unittest.IsolatedAsyncioTestCase):
    async def test_options_flow_constructs_and_uses_managed_entry(self):
        optional = Mock(side_effect=lambda key, default: (key, default))
        namespace = {
            "config_entries": SimpleNamespace(OptionsFlow=OptionsFlowDouble),
            "vol": SimpleNamespace(Schema=lambda schema: schema, Optional=optional, All=Mock(), Range=Mock()),
            "cv": SimpleNamespace(positive_int=int),
            "CONF_SCAN_INTERVAL": "scan_interval",
            "DEFAULT_SCAN_INTERVAL": 15,
            "callback": lambda function: function,
        }
        handler_class = load_definition("config_flow.py", "MediolaOptionsFlowHandler", namespace)
        factory = load_definition(
            "config_flow.py", "async_get_options_flow", namespace, parent="MediolaConfigFlow"
        )
        for options, data, expected in [
            ({"scan_interval": 45}, {"scan_interval": 30}, 45),
            ({}, {"scan_interval": 30}, 30),
            ({}, {}, 15),
        ]:
            with self.subTest(expected=expected):
                entry = SimpleNamespace(options=options, data=data)
                flow = factory(entry)
                self.assertIsInstance(flow, handler_class)
                flow.handler = "entry-id"
                lookup = Mock(return_value=entry)
                flow.hass = SimpleNamespace(config_entries=SimpleNamespace(async_get_known_entry=lookup))
                result = await flow.async_step_init()
                self.assertEqual(result["step_id"], "init")
                optional.assert_called_with("scan_interval", default=expected)
                self.assertEqual(
                    await flow.async_step_init({"scan_interval": 60}),
                    {"title": "", "data": {"scan_interval": 60}},
                )

    async def test_setup_registers_gateway_before_platforms_and_unloads(self):
        entry = SimpleNamespace(
            entry_id="entry-id",
            data={"host": "192.0.2.1", "username": "user", "password": "secret"},
            options={"scan_interval": 45},
            async_on_unload=Mock(),
            add_update_listener=Mock(),
        )
        api = SimpleNamespace(get_states=Mock(return_value=[]))
        hass = SimpleNamespace(data={}, async_add_executor_job=AsyncMock(side_effect=lambda fn: fn()))
        registry = SimpleNamespace(async_get_or_create=Mock(return_value=SimpleNamespace(id="gateway-id")))
        namespace = {
            "DataUpdateCoordinator": CoordinatorDouble,
            "DOMAIN": "mediola_shutters",
            "_LOGGER": Mock(),
            "timedelta": timedelta,
            "UpdateFailed": RuntimeError,
            "CONF_HOST": "host", "CONF_USERNAME": "username", "CONF_PASSWORD": "password",
            "CONF_SCAN_INTERVAL": "scan_interval", "DEFAULT_SCAN_INTERVAL": 15,
            "MediolaAPI": Mock(return_value=api),
            "dr": SimpleNamespace(async_get=Mock(return_value=registry)),
            "PLATFORMS": ["cover", "sensor", "binary_sensor"],
            "async_setup_services": AsyncMock(),
            "async_reload_entry": AsyncMock(),
        }
        coordinator_class = load_definition("__init__.py", "MediolaDataUpdateCoordinator", namespace)

        async def forward(actual_entry, platforms):
            coordinator = hass.data["mediola_shutters"][entry.entry_id]
            self.assertIs(actual_entry, entry)
            self.assertEqual(platforms, namespace["PLATFORMS"])
            self.assertEqual(coordinator.gateway_device_id, "gateway-id")

        hass.config_entries = SimpleNamespace(
            async_forward_entry_setups=AsyncMock(side_effect=forward),
            async_unload_platforms=AsyncMock(return_value=True),
        )
        setup = load_definition("__init__.py", "async_setup_entry", namespace)
        self.assertTrue(await setup(hass, entry))
        coordinator = hass.data["mediola_shutters"][entry.entry_id]
        self.assertIsInstance(coordinator, coordinator_class)
        self.assertIs(coordinator.config_entry, entry)
        self.assertIs(coordinator.api, api)
        self.assertEqual(coordinator.update_interval, timedelta(seconds=45))
        self.assertEqual(coordinator.data, [])
        registry.async_get_or_create.assert_called_once_with(
            config_entry_id="entry-id", identifiers={("mediola_shutters", "entry-id")},
            manufacturer="Mediola", name="Mediola Gateway (192.0.2.1)", model="Gateway",
        )
        unload = load_definition("__init__.py", "async_unload_entry", namespace)
        self.assertTrue(await unload(hass, entry))
        self.assertNotIn(entry.entry_id, hass.data["mediola_shutters"])

    async def test_device_info_preserves_identifiers_and_links_gateway(self):
        for filename, class_name in [
            ("cover.py", "MediolaCover"),
            ("sensor.py", "MediolaPositionSensor"),
            ("binary_sensor.py", "MediolaOpeningSensor"),
        ]:
            with self.subTest(platform=filename):
                namespace = {"DeviceInfo": DeviceInfoDouble, "DOMAIN": "mediola_shutters"}
                device_info = load_definition(filename, "device_info", namespace, parent=class_name)
                entity = SimpleNamespace(
                    _entry=SimpleNamespace(entry_id="entry-id"), _sid="01", _device_type="WR",
                    coordinator=SimpleNamespace(
                        gateway_device_id="gateway-id",
                        api=SimpleNamespace(get_manufacturer=Mock(return_value="WIR")),
                    ),
                )
                info = device_info.fget(entity)
                self.assertIsInstance(info, DeviceInfoDouble)
                self.assertEqual(info.identifiers, {("mediola_shutters", "entry-id_01")})
                self.assertEqual(info.via_device_id, "gateway-id")
                self.assertEqual((info.name, info.manufacturer, info.model), ("Shutter 01", "WIR", "WR Shutter"))

    async def test_closing_percentage_does_not_change_cover_state(self):
        attributes = load_definition(
            "cover.py", "extra_state_attributes", {}, parent="MediolaCover"
        )
        closed = load_definition("cover.py", "is_closed", {}, parent="MediolaCover")
        for position, expected, is_closed in [
            (100, 0, False), (0, 100, True), (75, 25, False), (None, None, None),
        ]:
            with self.subTest(position=position):
                entity = SimpleNamespace(current_cover_position=position)
                self.assertEqual(attributes.fget(entity), {
                    "closing_percentage": expected,
                    "closing_percentage_display": None if expected is None else f"{expected} %",
                })
                self.assertEqual(closed.fget(entity), is_closed)
                self.assertEqual(entity.current_cover_position, position)

    async def test_cover_commands_and_position_conversion(self):
        api = SimpleNamespace(**{name: Mock(return_value=True) for name in [
            "open_shutter", "close_shutter", "stop_shutter", "set_shutter_position"
        ]})
        executor = AsyncMock(side_effect=lambda fn, *args: fn(*args))
        refresh = AsyncMock()
        entity = SimpleNamespace(
            _device_type="WR", _sid="01", _adr="ABC",
            hass=SimpleNamespace(async_add_executor_job=executor),
            coordinator=SimpleNamespace(api=api, async_request_refresh=refresh),
        )
        for action in ["open", "close", "stop", "set_cover_position"]:
            method = "async_set_cover_position" if action == "set_cover_position" else f"async_{action}_cover"
            function = load_definition("cover.py", method, {
                "DEVICE_TYPE_WR": "WR", "ATTR_POSITION": "position", "_LOGGER": Mock(),
            }, parent="MediolaCover")
            await function(entity, position=25)
        api.open_shutter.assert_called_once_with("WR", "01", "ABC")
        api.close_shutter.assert_called_once_with("WR", "01", "ABC")
        api.stop_shutter.assert_called_once_with("WR", "01", "ABC")
        api.set_shutter_position.assert_called_once_with("WR", "01", "ABC", 75)
        self.assertEqual(refresh.await_count, 4)


if __name__ == "__main__":
    unittest.main()