"""Service errors: a status code + a sentence that tells the user what to do."""

from __future__ import annotations

from fastapi import HTTPException


class ServiceError(HTTPException):
    status = 400

    def __init__(self, detail: str, status: int | None = None) -> None:
        super().__init__(status or self.status, detail)

    def __str__(self) -> str:  # Telegram / CLI print the sentence, not "400: …"
        return str(self.detail)


class BadRequest(ServiceError):
    status = 400


class Forbidden(ServiceError):
    status = 403


class NotFound(ServiceError):
    status = 404


class Conflict(ServiceError):
    status = 409
