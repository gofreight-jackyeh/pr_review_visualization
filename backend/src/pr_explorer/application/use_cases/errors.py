from __future__ import annotations


class AppError(Exception):
    code = "internal"
    http_status = 500

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class InvalidInput(AppError):
    code = "invalid_input"
    http_status = 400


class NotFound(AppError):
    code = "not_found"
    http_status = 404


class AcquireFailed(AppError):
    code = "acquire_failed"
    http_status = 400


class Forbidden(AppError):
    code = "forbidden"
    http_status = 403


class Conflict(AppError):
    code = "conflict"
    http_status = 409
