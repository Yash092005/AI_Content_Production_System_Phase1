import copy

from app.config.schema import get_path, merge_defaults, set_path


class Settings:
    """An immutable view of one configuration version. `get("qa.max_revisions")` style access."""

    def __init__(self, data=None, version=None):
        self._d = merge_defaults(data or {})
        self.version = version

    def get(self, path, default=None):
        return get_path(self._d, path, default)

    def to_dict(self):
        return copy.deepcopy(self._d)

    def with_overrides(self, **overrides):
        """A copy with some values changed for ONE run, e.g. with_overrides(**{"system.pipeline_cache": False}).
        This is how "Fresh run" works now: no process-wide environment change."""
        d = self.to_dict()
        for path, value in overrides.items():
            set_path(d, path, value)
        return Settings(d, self.version)
