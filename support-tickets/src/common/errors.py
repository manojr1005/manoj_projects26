"""Domain errors. The HTTP layer maps them to a consistent error body."""


class AppError(Exception):
    status = 500
    code = "INTERNAL_ERROR"

    def __init__(self, message: str, details=None):
        super().__init__(message)
        self.message = message
        self.details = details


class ValidationError(AppError):
    status = 400
    code = "VALIDATION_ERROR"


class NotFoundError(AppError):
    status = 404
    code = "NOT_FOUND"


class UnauthorizedError(AppError):
    status = 401
    code = "UNAUTHORIZED"


class ServiceUnavailableError(AppError):
    status = 503
    code = "SERVICE_UNAVAILABLE"
