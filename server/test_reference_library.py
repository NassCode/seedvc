import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest

import reference_library


class ReferenceLibraryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        reference_library.ROOT = root
        reference_library.LIBRARY_DIR = root / "voices"
        reference_library.STATE_PATH = root / "voice-library.json"

    def tearDown(self):
        self.temporary.cleanup()

    def test_store_list_and_activate_persist(self):
        first = reference_library.ROOT / "first.wav"
        second = reference_library.ROOT / "second.wav"
        first.write_bytes(b"first")
        second.write_bytes(b"second")

        one = reference_library.store(first, "First voice", True)
        two = reference_library.store(second, "Second voice", False)
        state = reference_library.load_state()
        reference_library.find_voice(state, two["id"])
        state["active_id"] = two["id"]
        reference_library.save_state(state)

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            reference_library.command_list()
        listing = json.loads(output.getvalue())

        self.assertEqual(listing["active_id"], two["id"])
        self.assertEqual([voice["name"] for voice in listing["voices"]], ["First voice", "Second voice"])
        self.assertTrue((reference_library.LIBRARY_DIR / one["file"]).is_file())


if __name__ == "__main__":
    unittest.main()
