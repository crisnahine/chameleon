from __future__ import annotations


class UserService:
    def __init__(self) -> None:
        self._users: dict[int, dict] = {}
        self._next_id = 1

    def register_user(self, email: str, name: str) -> dict:
        user = {"id": self._next_id, "email": email.strip().lower(), "name": name.strip()}
        self._users[user["id"]] = user
        self._next_id += 1
        return user

    def get_user(self, user_id: int) -> dict | None:
        return self._users.get(user_id)


_service = UserService()


def get_user_service() -> UserService:
    return _service
