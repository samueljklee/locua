"""Actionable errors shared by the library and all adapters."""

class LocuaError(RuntimeError):
    def __init__(self, code, message, remedy, *, exit_code=3, details=None):
        super().__init__(message)
        self.code, self.message, self.remedy = code, message, remedy
        self.exit_code, self.details = exit_code, details

    def as_dict(self):
        value = {"code": self.code, "message": self.message, "remedy": self.remedy}
        if self.details is not None:
            value["details"] = self.details
        return value

