"""注册密码的服务端规则（FR-001）。"""


def is_valid_registration_password(password: str) -> bool:
    """密码必须为 8--256 字符，且至少各含一个 ASCII 字母和数字。"""
    if not 8 <= len(password) <= 256:
        return False
    has_letter = any(("A" <= char <= "Z") or ("a" <= char <= "z") for char in password)
    has_digit = any("0" <= char <= "9" for char in password)
    return has_letter and has_digit
