def lookup(mapping, key, default=None):
    """Use the default only for missing keys, preserving all existing values."""
    return mapping.get(key) or default
