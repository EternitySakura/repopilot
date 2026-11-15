class ToolFailure(Exception):
    def __init__(self, code: str, message: str, *, suggested_action: str | None = None,
                 artifact_ref: str | None = None):
        super().__init__(message)
        self.code = code
        self.suggested_action = suggested_action
        self.artifact_ref = artifact_ref
