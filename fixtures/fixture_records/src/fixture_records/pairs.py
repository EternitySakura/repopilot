def parse_pair(line):
    """Split a key/value record at its first equals sign and strip whitespace."""
    key, value = line.split("=")
    return key.strip(), value.strip()
