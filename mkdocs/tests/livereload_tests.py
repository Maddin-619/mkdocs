#!/usr/bin/env python

import contextlib
import email
import io
import os
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from mkdocs.livereload import LiveReloadServer
from mkdocs.tests.base import tempdir


class FakeRequest:
    def __init__(self, content):
        self.in_file = io.BytesIO(content.encode())
        self.out_file = io.BytesIO()
        self.out_file.close = lambda: None

    def makefile(self, *args, **kwargs):
        return self.in_file

    def sendall(self, data):
        self.out_file.write(data)


@contextlib.contextmanager
def testing_server(root, builder=lambda: None, mount_path="/"):
    """Create the server and start most of its parts, but don't listen on a socket."""
    with mock.patch("socket.socket"):
        server = LiveReloadServer(
            builder,
            host="localhost",
            port=0,
            root=root,
            mount_path=mount_path,
            build_delay=0.1,
            bind_and_activate=False,
        )
        server.setup_environ()
    server.observer.start()
    thread = threading.Thread(target=server._build_loop, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    thread.join()


def do_request(server, content):
    request = FakeRequest(content + " HTTP/1.1")
    server.RequestHandlerClass(request, ("127.0.0.1", 0), server)
    response = request.out_file.getvalue()

    headers, _, content = response.partition(b"\r\n\r\n")
    status, _, headers = headers.partition(b"\r\n")
    status = status.split(None, 1)[1].decode()

    headers = email.message_from_bytes(headers)
    headers["_status"] = status
    return headers, content.decode()


SCRIPT_REGEX = (
    r'<script src="/js/livereload.js"></script><script>livereload\([0-9]+, [0-9]+\);</script>'
)


class BuildTests(unittest.TestCase):
    @tempdir({"test.css": "div { color: red; }"})
    def test_serves_normal_file(self, site_dir):
        with testing_server(site_dir) as server:
            headers, output = do_request(server, "GET /test.css")
            self.assertEqual(output, "div { color: red; }")
            self.assertEqual(headers["_status"], "200 OK")
            self.assertEqual(headers.get("content-length"), str(len(output)))

    @tempdir({"docs/foo.docs": "docs1", "mkdocs.yml": "yml1"})
    @tempdir({"foo.site": "original"})
    def test_basic_rebuild(self, site_dir, origin_dir):
        docs_dir = Path(origin_dir, "docs")

        started_building = threading.Event()

        def rebuild():
            started_building.set()
            Path(site_dir, "foo.site").write_text(
                Path(docs_dir, "foo.docs").read_text() + Path(origin_dir, "mkdocs.yml").read_text()
            )

        with testing_server(site_dir, rebuild) as server:
            server.watch(docs_dir, rebuild)
            server.watch(Path(origin_dir, "mkdocs.yml"), rebuild)
            time.sleep(0.01)

            _, output = do_request(server, "GET /foo.site")
            self.assertEqual(output, "original")

            Path(docs_dir, "foo.docs").write_text("docs2")
            self.assertTrue(started_building.wait(timeout=10))
            started_building.clear()

            _, output = do_request(server, "GET /foo.site")
            self.assertEqual(output, "docs2yml1")

            Path(origin_dir, "mkdocs.yml").write_text("yml2")
            self.assertTrue(started_building.wait(timeout=10))
            started_building.clear()

            _, output = do_request(server, "GET /foo.site")
            self.assertEqual(output, "docs2yml2")

    @tempdir({"foo.docs": "a"})
    @tempdir({"foo.site": "original"})
    def test_rebuild_after_delete(self, site_dir, docs_dir):
        started_building = threading.Event()

        def rebuild():
            started_building.set()
            Path(site_dir, "foo.site").unlink()

        with testing_server(site_dir, rebuild) as server:
            server.watch(docs_dir, rebuild)
            time.sleep(0.01)

            Path(docs_dir, "foo.docs").write_text("b")
            self.assertTrue(started_building.wait(timeout=10))

            with self.assertLogs("mkdocs.livereload"):
                _, output = do_request(server, "GET /foo.site")

            self.assertIn("404", output)

    @tempdir({"aaa": "something"})
    def test_rebuild_after_rename(self, site_dir):
        started_building = threading.Event()

        with testing_server(site_dir, started_building.set) as server:
            server.watch(site_dir)
            time.sleep(0.01)

            Path(site_dir, "aaa").rename(Path(site_dir, "bbb"))
            self.assertTrue(started_building.wait(timeout=10))

    @tempdir()
    def test_no_rebuild_on_edit(self, site_dir):
        started_building = threading.Event()

        with open(Path(site_dir, "test"), "wb") as f:
            time.sleep(0.01)

            with testing_server(site_dir, started_building.set) as server:
                server.watch(site_dir)
                time.sleep(0.01)

                f.write(b"hi\n")
                f.flush()

                self.assertFalse(started_building.wait(timeout=0.2))

    @tempdir({"foo.docs": "a"})
    @tempdir({"foo.site": "original"})
    def test_custom_action_warns(self, site_dir, docs_dir):
        started_building = threading.Event()

        def rebuild():
            started_building.set()
            content = Path(docs_dir, "foo.docs").read_text()
            Path(site_dir, "foo.site").write_text(content * 5)

        with testing_server(site_dir) as server:
            with self.assertWarnsRegex(DeprecationWarning, "func") as cm:
                server.watch(docs_dir, rebuild)
                time.sleep(0.01)
            self.assertIn("livereload_tests.py", cm.filename)

            Path(docs_dir, "foo.docs").write_text("b")
            self.assertTrue(started_building.wait(timeout=10))

            _, output = do_request(server, "GET /foo.site")
            self.assertEqual(output, "bbbbb")

    @tempdir({"foo.docs": "docs1"})
    @tempdir({"foo.extra": "extra1"})
    @tempdir({"foo.site": "original"})
    def test_multiple_dirs_can_cause_rebuild(self, site_dir, extra_dir, docs_dir):
        started_building = threading.Barrier(2)

        def rebuild():
            started_building.wait(timeout=10)
            content1 = Path(docs_dir, "foo.docs").read_text()
            content2 = Path(extra_dir, "foo.extra").read_text()
            Path(site_dir, "foo.site").write_text(content1 + content2)

        with testing_server(site_dir, rebuild) as server:
            server.watch(docs_dir)
            server.watch(extra_dir)
            time.sleep(0.01)

            Path(docs_dir, "foo.docs").write_text("docs2")
            started_building.wait(timeout=10)

            _, output = do_request(server, "GET /foo.site")
            self.assertEqual(output, "docs2extra1")

            Path(extra_dir, "foo.extra").write_text("extra2")
            started_building.wait(timeout=10)

            _, output = do_request(server, "GET /foo.site")
            self.assertEqual(output, "docs2extra2")

    @tempdir({"foo.docs": "docs1"})
    @tempdir({"foo.extra": "extra1"})
    @tempdir({"foo.site": "original"})
    def test_multiple_dirs_changes_rebuild_only_once(self, site_dir, extra_dir, docs_dir):
        started_building = threading.Event()

        def rebuild():
            self.assertFalse(started_building.is_set())
            started_building.set()
            content1 = Path(docs_dir, "foo.docs").read_text()
            content2 = Path(extra_dir, "foo.extra").read_text()
            Path(site_dir, "foo.site").write_text(content1 + content2)

        with testing_server(site_dir, rebuild) as server:
            server.watch(docs_dir)
            server.watch(extra_dir)
            time.sleep(0.01)

            _, output = do_request(server, "GET /foo.site")
            Path(docs_dir, "foo.docs").write_text("docs2")
            Path(extra_dir, "foo.extra").write_text("extra2")
            self.assertTrue(started_building.wait(timeout=10))

            _, output = do_request(server, "GET /foo.site")
            self.assertEqual(output, "docs2extra2")

    @tempdir({"foo.docs": "a"})
    @tempdir({"foo.site": "original"})
    def test_change_is_detected_while_building(self, site_dir, docs_dir):
        before_finished_building = threading.Barrier(2)
        can_finish_building = threading.Event()

        def rebuild():
            content = Path(docs_dir, "foo.docs").read_text()
            Path(site_dir, "foo.site").write_text(content * 5)
            before_finished_building.wait(timeout=10)
            self.assertTrue(can_finish_building.wait(timeout=10))

        with testing_server(site_dir, rebuild) as server:
            server.watch(docs_dir)
            time.sleep(0.01)

            Path(docs_dir, "foo.docs").write_text("b")
            before_finished_building.wait(timeout=10)
            Path(docs_dir, "foo.docs").write_text("c")
            can_finish_building.set()

            _, output = do_request(server, "GET /foo.site")
            self.assertEqual(output, "bbbbb")

            before_finished_building.wait(timeout=10)

            _, output = do_request(server, "GET /foo.site")
            self.assertEqual(output, "ccccc")

    @tempdir(
        {
            "normal.html": "<html><body>hello</body></html>",
            "no_body.html": "<p>hi",
            "empty.html": "",
            "multi_body.html": "<body>foo</body><body>bar</body>",
        }
    )
    def test_serves_modified_html(self, site_dir):
        with testing_server(site_dir) as server:
            headers, output = do_request(server, "GET /normal.html")
            self.assertRegex(output, fr"^<html><body>hello{SCRIPT_REGEX}</body></html>$")
            self.assertEqual(headers.get("content-type"), "text/html")
            self.assertEqual(headers.get("content-length"), str(len(output)))

            _, output = do_request(server, "GET /no_body.html")
            self.assertRegex(output, fr"^<p>hi{SCRIPT_REGEX}$")

            headers, output = do_request(server, "GET /empty.html")
            self.assertRegex(output, fr"^{SCRIPT_REGEX}$")
            self.assertEqual(headers.get("content-length"), str(len(output)))

            _, output = do_request(server, "GET /multi_body.html")
            self.assertRegex(output, fr"^<body>foo</body><body>bar{SCRIPT_REGEX}</body>$")

    @tempdir({"index.html": "<body>aaa</body>", "foo/index.html": "<body>bbb</body>"})
    def test_serves_modified_index(self, site_dir):
        with testing_server(site_dir) as server:
            headers, output = do_request(server, "GET /")
            self.assertRegex(output, fr"^<body>aaa{SCRIPT_REGEX}</body>$")
            self.assertEqual(headers["_status"], "200 OK")
            self.assertEqual(headers.get("content-type"), "text/html")
            self.assertEqual(headers.get("content-length"), str(len(output)))

            _, output = do_request(server, "GET /foo/")
            self.assertRegex(output, fr"^<body>bbb{SCRIPT_REGEX}</body>$")

    @tempdir({"я.html": "<body>aaa</body>", "测试2/index.html": "<body>bbb</body>"})
    def test_serves_with_unicode_characters(self, site_dir):
        with testing_server(site_dir) as server:
            _, output = do_request(server, "GET /я.html")
            self.assertRegex(output, fr"^<body>aaa{SCRIPT_REGEX}</body>$")
            _, output = do_request(server, "GET /%D1%8F.html")
            self.assertRegex(output, fr"^<body>aaa{SCRIPT_REGEX}</body>$")

            with self.assertLogs("mkdocs.livereload"):
                headers, _ = do_request(server, "GET /%D1.html")
            self.assertEqual(headers["_status"], "404 Not Found")

            _, output = do_request(server, "GET /测试2/")
            self.assertRegex(output, fr"^<body>bbb{SCRIPT_REGEX}</body>$")
            _, output = do_request(server, "GET /%E6%B5%8B%E8%AF%952/index.html")
            self.assertRegex(output, fr"^<body>bbb{SCRIPT_REGEX}</body>$")

    @tempdir()
    def test_serves_js(self, site_dir):
        with testing_server(site_dir) as server:
            for mount_path in "/", "/sub/":
                server.mount_path = mount_path

                headers, output = do_request(server, "GET /js/livereload.js")
                self.assertIn("function livereload", output)
                self.assertEqual(headers["_status"], "200 OK")
                self.assertEqual(headers.get("content-type"), "application/javascript")

    @tempdir()
    def test_serves_polling_instantly(self, site_dir):
        with testing_server(site_dir) as server:
            _, output = do_request(server, "GET /livereload/0/0")
            self.assertTrue(output.isdigit())

    @tempdir()
    @tempdir()
    def test_serves_polling_after_event(self, site_dir, docs_dir):
        with testing_server(site_dir) as server:
            initial_epoch = server._visible_epoch

            server.watch(docs_dir)
            time.sleep(0.01)

            Path(docs_dir, "foo.docs").write_text("b")

            _, output = do_request(server, f"GET /livereload/{initial_epoch}/0")

            self.assertNotEqual(server._visible_epoch, initial_epoch)
            self.assertEqual(output, str(server._visible_epoch))

    @tempdir()
    def test_serves_polling_with_timeout(self, site_dir):
        with testing_server(site_dir) as server:
            server.poll_response_timeout = 0.2
            initial_epoch = server._visible_epoch

            start_time = time.monotonic()
            _, output = do_request(server, f"GET /livereload/{initial_epoch}/0")
            self.assertGreaterEqual(time.monotonic(), start_time + 0.2)
            self.assertEqual(output, str(initial_epoch))

    @tempdir()
    def test_error_handler(self, site_dir):
        with testing_server(site_dir) as server:
            server.error_handler = lambda code: b"[%d]" % code
            with self.assertLogs("mkdocs.livereload") as cm:
                headers, output = do_request(server, "GET /missing")

            self.assertEqual(headers["_status"], "404 Not Found")
            self.assertEqual(output, "[404]")
            self.assertRegex(
                "\n".join(cm.output),
                r'^WARNING:mkdocs.livereload:.*"GET /missing HTTP/1.1" code 404',
            )

    @tempdir()
    def test_bad_error_handler(self, site_dir):
        self.maxDiff = None
        with testing_server(site_dir) as server:
            server.error_handler = lambda code: 0 / 0
            with self.assertLogs("mkdocs.livereload") as cm:
                headers, output = do_request(server, "GET /missing")

            self.assertEqual(headers["_status"], "404 Not Found")
            self.assertIn("404", output)
            self.assertRegex(
                "\n".join(cm.output), r"Failed to render an error message[\s\S]+/missing.+code 404"
            )

    @tempdir(
        {
            "test.html": "<!DOCTYPE html>\nhi",
            "test.xml": '<?xml version="1.0" encoding="UTF-8"?>\n<foo></foo>',
            "test.css": "div { color: red; }",
            "test.js": "use strict;",
            "test.json": '{"a": "b"}',
        }
    )
    def test_mime_types(self, site_dir):
        with testing_server(site_dir) as server:
            headers, _ = do_request(server, "GET /test.html")
            self.assertEqual(headers.get("content-type"), "text/html")

            headers, _ = do_request(server, "GET /test.xml")
            self.assertIn(headers.get("content-type"), ["text/xml", "application/xml"])

            headers, _ = do_request(server, "GET /test.css")
            self.assertEqual(headers.get("content-type"), "text/css")

            headers, _ = do_request(server, "GET /test.js")
            self.assertEqual(headers.get("content-type"), "application/javascript")

            headers, _ = do_request(server, "GET /test.json")
            self.assertEqual(headers.get("content-type"), "application/json")

    @tempdir({"index.html": "<body>aaa</body>", "sub/sub.html": "<body>bbb</body>"})
    def test_serves_from_mount_path(self, site_dir):
        with testing_server(site_dir, mount_path="/sub") as server:
            headers, output = do_request(server, "GET /sub/")
            self.assertRegex(output, fr"^<body>aaa{SCRIPT_REGEX}</body>$")
            self.assertEqual(headers.get("content-type"), "text/html")

            _, output = do_request(server, "GET /sub/sub/sub.html")
            self.assertRegex(output, fr"^<body>bbb{SCRIPT_REGEX}</body>$")

            with self.assertLogs("mkdocs.livereload"):
                headers, _ = do_request(server, "GET /sub/sub.html")
            self.assertEqual(headers["_status"], "404 Not Found")

    @tempdir()
    def test_redirects_to_mount_path(self, site_dir):
        with testing_server(site_dir, mount_path="/mount/path") as server:
            with self.assertLogs("mkdocs.livereload"):
                headers, _ = do_request(server, "GET /")
            self.assertEqual(headers["_status"], "302 Found")
            self.assertEqual(headers.get("location"), "/mount/path/")

    @tempdir({"mkdocs.yml": "original", "mkdocs2.yml": "original"}, prefix="tmp_dir")
    @tempdir(prefix="origin_dir")
    @tempdir({"subdir/foo.md": "original"}, prefix="dest_docs_dir")
    def test_watches_direct_symlinks(self, dest_docs_dir, origin_dir, tmp_dir):
        try:
            Path(origin_dir, "docs").symlink_to(dest_docs_dir, target_is_directory=True)
            Path(origin_dir, "mkdocs.yml").symlink_to(Path(tmp_dir, "mkdocs.yml"))
        except NotImplementedError:  # PyPy on Windows
            self.skipTest("Creating symlinks not supported")

        started_building = threading.Event()

        def wait_for_build():
            result = started_building.wait(timeout=10)
            started_building.clear()
            with self.assertLogs("mkdocs.livereload"):
                do_request(server, "GET /")
            return result

        with testing_server(tmp_dir, started_building.set) as server:
            server.watch(Path(origin_dir, "docs"))
            server.watch(Path(origin_dir, "mkdocs.yml"))
            time.sleep(0.01)

            Path(tmp_dir, "mkdocs.yml").write_text("edited")
            self.assertTrue(wait_for_build())

            Path(dest_docs_dir, "subdir", "foo.md").write_text("edited")
            self.assertTrue(wait_for_build())

            Path(origin_dir, "unrelated.md").write_text("foo")
            self.assertFalse(started_building.wait(timeout=0.2))

    @tempdir(["file_dest_1.md", "file_dest_2.md", "file_dest_unused.md"], prefix="tmp_dir")
    @tempdir(["file_under.md"], prefix="dir_to_link_to")
    @tempdir()
    def test_watches_through_symlinks(self, docs_dir, dir_to_link_to, tmp_dir):
        try:
            Path(docs_dir, "link1.md").symlink_to(Path(tmp_dir, "file_dest_1.md"))
            Path(docs_dir, "linked_dir").symlink_to(dir_to_link_to, target_is_directory=True)

            Path(dir_to_link_to, "sublink.md").symlink_to(Path(tmp_dir, "file_dest_2.md"))
        except NotImplementedError:  # PyPy on Windows
            self.skipTest("Creating symlinks not supported")

        started_building = threading.Event()

        def wait_for_build():
            result = started_building.wait(timeout=10)
            started_building.clear()
            with self.assertLogs("mkdocs.livereload"):
                do_request(server, "GET /")
            return result

        with testing_server(docs_dir, started_building.set) as server:
            server.watch(docs_dir)
            time.sleep(0.01)

            Path(tmp_dir, "file_dest_1.md").write_text("edited")
            self.assertTrue(wait_for_build())

            Path(dir_to_link_to, "file_under.md").write_text("edited")
            self.assertTrue(wait_for_build())

            Path(tmp_dir, "file_dest_2.md").write_text("edited")
            self.assertTrue(wait_for_build())

            Path(docs_dir, "link1.md").unlink()
            self.assertTrue(wait_for_build())

            Path(tmp_dir, "file_dest_unused.md").write_text("edited")
            self.assertFalse(started_building.wait(timeout=0.2))

    @tempdir(prefix="site_dir")
    @tempdir(["docs/unused.md", "README.md"], prefix="origin_dir")
    def test_watches_through_relative_symlinks(self, origin_dir, site_dir):
        docs_dir = Path(origin_dir, "docs")
        old_cwd = os.getcwd()
        os.chdir(docs_dir)
        try:
            Path(docs_dir, "README.md").symlink_to(Path("..", "README.md"))
        except NotImplementedError:  # PyPy on Windows
            self.skipTest("Creating symlinks not supported")
        finally:
            os.chdir(old_cwd)

        started_building = threading.Event()

        with testing_server(docs_dir, started_building.set) as server:
            server.watch(docs_dir)
            time.sleep(0.01)

            Path(origin_dir, "README.md").write_text("edited")
            self.assertTrue(started_building.wait(timeout=10))

    @tempdir()
    def test_watch_with_broken_symlinks(self, docs_dir):
        Path(docs_dir, "subdir").mkdir()

        try:
            if sys.platform != "win32":
                Path(docs_dir, "subdir", "circular").symlink_to(Path(docs_dir))

            Path(docs_dir, "broken_1").symlink_to(Path(docs_dir, "oh no"))
            Path(docs_dir, "broken_2").symlink_to(Path(docs_dir, "oh no"), target_is_directory=True)
            Path(docs_dir, "broken_3").symlink_to(Path(docs_dir, "broken_2"))
        except NotImplementedError:  # PyPy on Windows
            self.skipTest("Creating symlinks not supported")

        started_building = threading.Event()
        with testing_server(docs_dir, started_building.set) as server:
            server.watch(docs_dir)
            time.sleep(0.01)

            Path(docs_dir, "subdir", "test").write_text("test")
            self.assertTrue(started_building.wait(timeout=10))
