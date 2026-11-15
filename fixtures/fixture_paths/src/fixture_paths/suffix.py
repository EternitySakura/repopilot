def suffix(name):
    """Return a basename's final extension without a dot; dotfiles have no extension."""
    return name.rsplit(".", 1)[1] if "." in name else ""
