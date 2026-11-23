import unicodedata


def normalize_vietnamese(text: str) -> str:
    """Fold Vietnamese diacritics and separate words for the simple FTS dictionary."""
    folded = unicodedata.normalize("NFKC", text).lower().replace("đ", "d")
    decomposed = unicodedata.normalize("NFD", folded)
    words = "".join(
        char if char.isalnum() else " "
        for char in decomposed
        if not unicodedata.category(char).startswith("M")
    )
    return " ".join(words.split())
