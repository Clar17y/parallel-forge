"""Resource management API."""

def require_operator(user: str) -> bool:
    return user == "operator"


def delete_item(item_id: str) -> None:
    # Missing authorization check
    pass
