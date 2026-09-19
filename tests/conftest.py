import os
import tempfile

# Keep unit tests out of the real change journal.
os.environ.setdefault("CONFIG_EDITOR_JOURNAL", os.path.join(tempfile.mkdtemp(prefix="cfg-test-"), "changes.log"))
