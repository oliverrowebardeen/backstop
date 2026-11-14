import os

# S4 tests run on the fakes only (no keys, no paid calls). Must be set before
# evals.deps is imported.
os.environ["SO_FAKE"] = "1"
