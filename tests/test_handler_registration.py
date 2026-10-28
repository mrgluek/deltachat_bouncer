"""Event hooks registered with @dc_cli.on() are called as hook(bot, accid, event).

Checked on the source with ast, so no Delta Chat stack is needed. From v2.38.0 to v2.44.2 a
helper was inserted between the info-message decorator and its handler: the decorator took the
helper, every info message raised a TypeError and handle_dc_info_message never ran.
"""
import ast
import glob
import os
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def _hooks():
    """(file, function node) for every function decorated with <something>.dc_cli.on(...)."""
    for path in sorted(glob.glob(os.path.join(ROOT, "*.py"))):
        tree = ast.parse(open(path, encoding="utf-8").read(), filename=path)
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for dec in node.decorator_list:
                func = dec.func if isinstance(dec, ast.Call) else dec
                if isinstance(func, ast.Attribute) and func.attr == "on" and ast.unparse(func.value).endswith("dc_cli"):
                    yield os.path.basename(path), node, dec


class TestHandlerRegistration(unittest.TestCase):
    def test_hooks_take_bot_accid_event(self):
        hooks = list(_hooks())
        self.assertTrue(hooks)
        for fname, node, _ in hooks:
            with self.subTest(hook=f"{fname}:{node.name}"):
                args = node.args.args
                self.assertEqual([a.arg for a in args[:3]], ["bot", "accid", "event"])
                # Extra parameters (e.g. skip_cooldown for direct calls) need defaults
                self.assertGreaterEqual(len(node.args.defaults), len(args) - 3)

    def test_info_message_handler_is_registered(self):
        info_hooks = [node.name for _, node, dec in _hooks() if "is_info=True" in ast.unparse(dec)]
        self.assertIn("handle_dc_info_message", info_hooks)


if __name__ == "__main__":
    unittest.main()
