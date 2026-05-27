import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from text_extractor import extract_text


def test_plain_text():
    data = b"Hello, world! This is a plain text file."
    result = extract_text(data, ".txt")
    assert "Hello, world!" in result


def test_json_extraction():
    data = b'{"key": "value", "nested": {"attack": "ignore me"}}'
    result = extract_text(data, ".json")
    assert "value" in result
    assert "attack" in result


def test_unknown_extension_falls_back_to_plain():
    data = b"Some raw text content"
    result = extract_text(data, ".unknown")
    assert "Some raw text content" in result


def test_empty_bytes_returns_empty():
    result = extract_text(b"", ".txt")
    assert result == ""


def test_latin1_encoding():
    data = "Café au lait".encode("latin-1")
    result = extract_text(data, ".txt")
    assert "Caf" in result


def test_csv():
    data = b"name,email\nAlice,alice@example.com\nBob,bob@example.com"
    result = extract_text(data, ".csv")
    assert "Alice" in result
    assert "bob@example.com" in result
