"""Agent tools glue: the coroutine driver, screen capture, pure helpers and tool calls against fakes."""

import os
import tempfile
import unittest
from dataclasses import dataclass, field

import gi

gi.require_version("GdkPixbuf", "2.0")
from gi.repository import GdkPixbuf, GLib  # noqa: E402

from weitblick.agent import loop, screen_capture, tools  # noqa: E402
from weitblick.agent.screen_mapping import DesktopRect, PixelRect, ScreenMapping  # noqa: E402
from weitblick.agent.ui import setup_command  # noqa: E402
from weitblick.rdp.session import Frame  # noqa: E402
from weitblick.store import AgentAccess, Connection  # noqa: E402
from weitblick.ui.activity import CONNECTED, SessionStatus  # noqa: E402


def run_coroutine(coroutine, timeout=10.0):
    """Runs a coroutine on a fresh GLib main loop and returns its result."""
    main = GLib.MainLoop()
    task = loop.spawn(coroutine)
    task.add_done_callback(lambda _t: main.quit())
    source = GLib.timeout_add(int(timeout * 1000), main.quit)
    if not task.done:
        main.run()
    GLib.source_remove(source) if GLib.MainContext.default().find_source_by_id(source) else None
    return task.result()


def busy_frame(width, height):
    """A picture with many colors (not a blank screen still signing in)."""
    size = width * height * 4
    data = (bytes(range(251)) * (size // 251 + 1))[:size]
    return Frame(data, width, height, width * 4)


def frame(width, height, color=(10, 20, 30), stride=None):
    stride = stride or width * 4
    row = bytes(color + (255,)) * width + b"\0" * (stride - width * 4)
    return Frame(row * height, width, height, stride)


class LoopTests(unittest.TestCase):
    def test_sleep_callbacks_and_threads(self):
        async def body():
            await loop.sleep(0.01)
            value = await loop.from_callback(lambda done: GLib.idle_add(lambda: (done(41), False)[1]))
            doubled = await loop.run_in_thread(lambda x: x * 2, value + 1)
            return doubled
        self.assertEqual(run_coroutine(body()), 84)

    def test_exceptions_reach_the_awaiting_coroutine(self):
        async def failing():
            await loop.sleep(0)
            raise ValueError("boom")

        async def body():
            try:
                await loop.spawn(failing())
            except ValueError as error:
                return str(error)
        self.assertEqual(run_coroutine(body()), "boom")

    def test_serial_queue_runs_in_order(self):
        order = []
        queue = loop.SerialQueue()

        async def work(name, delay):
            await loop.sleep(delay)
            order.append(name)
            return name

        async def body():
            first = loop.spawn(queue.run(lambda: work("first", 0.05)))
            second = loop.spawn(queue.run(lambda: work("second", 0)))
            return [await first, await second]
        self.assertEqual(run_coroutine(body()), ["first", "second"])
        self.assertEqual(order, ["first", "second"])


class ScreenCaptureTests(unittest.TestCase):
    def test_crop_respects_stride_and_clips(self):
        source = frame(10, 6, stride=48)
        pixels = screen_capture.crop(source, DesktopRect(8, 4, 5, 5))
        self.assertEqual(pixels.rect, DesktopRect(8, 4, 2, 2))
        self.assertEqual(len(pixels.bgra), 2 * 2 * 4)
        self.assertIsNone(screen_capture.crop(source, DesktopRect(20, 20, 5, 5)))
        whole = screen_capture.crop(frame(4, 3))
        self.assertEqual(whole.rect, DesktopRect(0, 0, 4, 3))

    def test_bgra_to_rgb_swaps_channels(self):
        self.assertEqual(screen_capture.bgra_to_rgb(bytes([1, 2, 3, 255, 4, 5, 6, 0]), 2, 1), bytes([3, 2, 1, 6, 5, 4]))

    def test_blank_detection(self):
        self.assertTrue(screen_capture.looks_blank(frame(200, 100)))
        busy = bytearray(frame(200, 100).data)
        for index in range(0, len(busy), 4 * 7):
            busy[index:index + 3] = bytes([(index * 13) % 256, (index * 7) % 256, (index * 3) % 256])
        self.assertFalse(screen_capture.looks_blank(Frame(bytes(busy), 200, 100, 800)))
        self.assertTrue(screen_capture.looks_blank(None))

    def test_png_unless_too_large(self):
        pixels = screen_capture.crop(frame(64, 32))
        data, mime = screen_capture.screenshot(pixels, 32, 16)
        self.assertEqual(mime, "image/png")
        loader = GdkPixbuf.PixbufLoader()
        loader.write(data)
        loader.close()
        self.assertEqual((loader.get_pixbuf().get_width(), loader.get_pixbuf().get_height()), (32, 16))
        data, mime = screen_capture.encode(screen_capture.pixbuf(pixels), png_limit=10)
        self.assertEqual(mime, "image/jpeg")
        self.assertTrue(data.startswith(b"\xff\xd8"))

    def test_ocr_png_is_upscaled(self):
        png = screen_capture.ocr_png(screen_capture.crop(frame(20, 10)), 2)
        loader = GdkPixbuf.PixbufLoader()
        loader.write(png)
        loader.close()
        self.assertEqual(loader.get_pixbuf().get_width(), 40)


class HelperTests(unittest.TestCase):
    def test_points_and_sizes(self):
        mapping = ScreenMapping(2560, 1600)
        self.assertEqual(tools.describe_point((1280, 800), mapping), "(640, 400)")
        self.assertEqual(tools.screenshot_size(DesktopRect(0, 0, 2560, 1600), mapping), (1280, 800))
        self.assertEqual(tools.describe_box(PixelRect(1.4, 2.5, 10, 20)), "(1, 3, 10×20)")
        self.assertEqual(tools.click_point(PixelRect(10, 10, 10, 20)), "click (15, 20)")

    def test_drag_and_wheel(self):
        steps = tools.drag_steps((0, 0), (64, 0))
        self.assertEqual(steps[-1], (64, 0))
        self.assertEqual(len(steps), 4)
        self.assertEqual(tools.drag_steps((5, 5), (5, 5)), [(5, 5)])
        self.assertEqual(tools.wheel_events(1, 2), [(-120, 0), (-120, 0), (0, 120)])
        self.assertEqual(tools.wheel_events(0, -1), [(120, 0)])
        self.assertEqual(tools.scroll_description(-2, 3), "3 notches down and 2 notches left")

    def test_status_names_are_english(self):
        self.assertEqual(tools.status_name(SessionStatus("reconnecting", 2)), "reconnecting (attempt 2)")
        self.assertEqual(tools.status_name(CONNECTED), "connected")

    def test_setup_command(self):
        self.assertEqual(setup_command(which=lambda _n: "/opt/bin/weitblick-mcp"),
                         "claude mcp add weitblick -- /opt/bin/weitblick-mcp")
        with tempfile.TemporaryDirectory() as home:
            os.makedirs(os.path.join(home, ".local", "bin"))
            launcher = os.path.join(home, ".local", "bin", "weitblick-mcp")
            open(launcher, "w").close()
            self.assertIn(launcher, setup_command(which=lambda _n: None, home=__import__("pathlib").Path(home)))
            command = setup_command(which=lambda _n: None, home=__import__("pathlib").Path(home, "x"),
                                    executable="/usr/bin/python3", source_root=__import__("pathlib").Path("/src/wb"))
        self.assertEqual(command, "claude mcp add weitblick -e PYTHONPATH=/src/wb -- /usr/bin/python3 -m "
                                  "weitblick.mcp_helper")


# MARK: - Tool calls against fakes

@dataclass
class FakeConfig:
    keyboard_layout: int = 0x0407


class FakeSession:
    def __init__(self, width=1280, height=800):
        self.desktop_size = (width, height)
        self.config = FakeConfig()
        self.events = []
        self.frame = busy_frame(width, height)

    def peek_frame(self):
        return self.frame

    def send_scancode(self, code, extended, down):
        self.events.append(("key", code, extended, down))

    def send_unicode_unit(self, unit, down):
        self.events.append(("unicode", unit, down))

    def send_pause(self):
        self.events.append(("pause",))

    def send_mouse_move(self, x, y):
        self.events.append(("move", x, y))

    def send_mouse_button(self, button, down, x, y):
        self.events.append(("button", int(button), down, x, y))

    def send_mouse_wheel(self, vertical, horizontal, x, y):
        self.events.append(("wheel", vertical, horizontal, x, y))


@dataclass
class FakeControl:
    client: str
    paused: bool = False


class FakePage:
    def __init__(self, connection, session_id, session=None, shown=False):
        self.connection = connection
        self.connection_id = connection.id
        self.session_id = session_id
        self.status = CONNECTED
        self.connected_session = session or FakeSession()
        self.clipboard = None
        self.agent_control = None
        self.is_shown = shown
        self.agent_end_message = None
        self.closed = False

    def attach_agent(self, client):
        self.agent_control = FakeControl(client)

    def detach_agent(self):
        self.agent_control = None

    def wait_until_connected(self, _timeout, callback):
        callback(None)


class FakeLibrary:
    def __init__(self, connections):
        self.connections = connections

    def search(self, _query="", _sort="name"):
        return sorted(self.connections, key=lambda c: c.name)

    def connection(self, connection_id):
        return next((c for c in self.connections if c.id == connection_id), None)

    def password(self, _connection_id):
        return "secret"

    def modify(self, connection_id, change):
        connection = self.connection(connection_id)
        change(connection)
        return connection


class FakeApp:
    def __init__(self, connections):
        self.library = FakeLibrary(connections)
        self.pages = []

    def session_pages(self):
        return list(self.pages)

    def open_agent_session(self, connection, width, height, show, session_id):
        page = FakePage(connection, session_id, FakeSession(width, height), shown=show)
        self.pages.append(page)
        return page

    def show_session_page(self, page, activate=True):
        page.is_shown = True

    def close_session_page(self, page):
        page.closed = True
        if page in self.pages:
            self.pages.remove(page)

    def get_windows(self):
        return []

    def quit(self):
        pass


class FakeSocket:
    def __init__(self, connection_id=1):
        self.id = connection_id
        self.sent = []

    def send(self, line):
        self.sent.append(line)

    def close(self):
        pass


class ToolCallTests(unittest.TestCase):
    def setUp(self):
        self.state = tempfile.TemporaryDirectory()
        self.previous_state = os.environ.get("XDG_STATE_HOME")
        os.environ["XDG_STATE_HOME"] = self.state.name
        from weitblick.agent.host import AgentClient, AgentHost
        self.office = Connection(name="Office PC", host="office.example", username="agent")
        self.office.security.agent_access = AgentAccess.ALWAYS
        self.locked = Connection(name="Production", host="prod.example", username="agent")
        self.locked.security.agent_access = AgentAccess.NEVER
        self.app = FakeApp([self.office, self.locked])
        self.host = AgentHost(self.app)
        self.client = AgentClient(FakeSocket())
        self.host.clients[self.client.id] = self.client

    def tearDown(self):
        self.host.log.flush(2)
        if self.previous_state is None:
            os.environ.pop("XDG_STATE_HOME", None)
        else:
            os.environ["XDG_STATE_HOME"] = self.previous_state
        self.state.cleanup()

    def call(self, name, arguments):
        result = run_coroutine(self.host.tools.call(name, arguments, self.client), timeout=20)
        texts = "\n".join(block.text for block in result.content if hasattr(block, "text"))
        return result, texts

    def connect(self):
        result, text = self.call("connect", {"connection": "office pc"})
        self.assertFalse(result.is_error, text)
        return self.app.pages[-1]

    def test_list_connections_and_never(self):
        result, text = self.call("list_connections", {})
        self.assertIn('"agent_access":"always"', text.replace(" ", ""))
        self.assertIn("Production", text)
        result, text = self.call("connect", {"connection": "Production"})
        self.assertTrue(result.is_error)
        self.assertIn("Never", text)

    def test_connect_click_and_screenshot(self):
        page = self.connect()
        self.assertEqual(page.agent_control.client, self.client.name)
        session = page.connected_session
        result, text = self.call("click", {"session": page.session_id, "x": 100, "y": 50, "count": 2,
                                           "modifiers": ["ctrl"]})
        self.assertFalse(result.is_error, text)
        self.assertIn("Double-clicked at (100, 50)", text)
        buttons = [event for event in session.events if event[0] == "button"]
        self.assertEqual(len(buttons), 4)
        self.assertEqual(session.events[0], ("key", 0x1D, False, True))
        self.assertEqual(session.events[-1], ("key", 0x1D, False, False))
        result, text = self.call("screenshot", {"session": page.session_id})
        self.assertFalse(result.is_error, text)
        self.assertEqual(result.content[0].mime_type, "image/png")
        self.assertIn("1280×800 px, scale 1", text)
        self.assertIn("Pointer at (100, 50)", text)

    def test_keys_resolve_through_the_session_layout(self):
        page = self.connect()
        session = page.connected_session
        result, text = self.call("key", {"session": page.session_id, "keys": "ctrl+z"})
        self.assertFalse(result.is_error, text)
        keys = [event for event in session.events if event[0] == "key"]
        # German layout: the key that types z is the physical Y key (0x15).
        self.assertIn(("key", 0x15, False, True), keys)
        result, text = self.call("key", {"session": page.session_id, "keys": "cmd+c"})
        self.assertTrue(result.is_error)
        self.assertIn("no Cmd key", text)

    def test_type_text_and_scroll(self):
        page = self.connect()
        session = page.connected_session
        result, text = self.call("type_text", {"session": page.session_id, "text": "Hä\n"})
        self.assertIn("Typed 3 characters", text)
        units = [event[1] for event in session.events if event[0] == "unicode" and event[2]]
        self.assertEqual(units, [ord("H"), ord("ä")])
        self.assertIn(("key", 0x1C, False, True), session.events)
        result, text = self.call("scroll", {"session": page.session_id, "x": 10, "y": 10, "dy": 2})
        self.assertEqual([event[1] for event in session.events if event[0] == "wheel"], [-120, -120])
        result, text = self.call("scroll", {"session": page.session_id, "x": 10, "y": 10})
        self.assertTrue(result.is_error)

    def test_paused_and_ended_sessions(self):
        page = self.connect()
        page.agent_control.paused = True
        result, text = self.call("screenshot", {"session": page.session_id})
        self.assertTrue(result.is_error)
        self.assertIn("Paused by the user", text)
        page.agent_control.paused = False
        result, text = self.call("disconnect", {"session": page.session_id})
        self.assertIn("Disconnected session", text)
        self.assertTrue(page.closed)
        result, text = self.call("screenshot", {"session": page.session_id})
        self.assertTrue(result.is_error)

    def test_attach_to_the_users_session(self):
        user_page = FakePage(self.office, "abc123", shown=True)
        self.app.pages.append(user_page)
        result, text = self.call("connect", {"connection": "Office PC"})
        self.assertFalse(result.is_error, text)
        self.assertIn("Attached to the user's open session", text)
        result, text = self.call("disconnect", {"session": "abc123"})
        self.assertIn("stays open for the user", text)
        self.assertIsNone(user_page.agent_control)
        self.assertFalse(user_page.closed)

    def test_client_gone_closes_owned_sessions(self):
        page = self.connect()
        self.host._client_gone(self.client)
        self.assertTrue(page.closed)
        self.assertEqual(self.host.records, {})

    def test_audit_logs_typed_text_by_length(self):
        page = self.connect()
        self.call("type_text", {"session": page.session_id, "text": "geheim"})
        self.host.log.flush(2)
        with open(self.host.log.path, encoding="utf-8") as handle:
            content = handle.read()
        self.assertIn("tool=type_text", content)
        self.assertNotIn("geheim", content)


if __name__ == "__main__":
    unittest.main()
