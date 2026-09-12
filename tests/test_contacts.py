from __future__ import annotations

import pytest

from core.contacts import ContactAmbiguous, ContactsBook


def test_contact_persists_and_resolves_by_alias_and_channel(tmp_path):
    book = ContactsBook(tmp_path / "contacts.json")
    saved = book.save(
        name="Alice Martin", aliases=["Lili"], emails=["alice@example.com"],
        phone="+224600000000", handles={"telegram": "@alice"},
    )

    assert book.resolve("lili", "email").value == "alice@example.com"
    assert book.resolve("Alice", "telegram").value == "@alice"
    assert book.resolve("Alice Martin", "whatsapp").value == "+224600000000"
    assert ContactsBook(tmp_path / "contacts.json").list()[0]["id"] == saved["id"]
    assert (tmp_path / "contacts.json").stat().st_mode & 0o777 == 0o600


def test_contact_resolution_refuses_ambiguity(tmp_path):
    book = ContactsBook(tmp_path / "contacts.json")
    book.save(name="Alice Martin", emails=["a@example.com"])
    book.save(name="Alice Diallo", emails=["b@example.com"])

    with pytest.raises(ContactAmbiguous):
        book.resolve("Alice", "email")


def test_contact_update_and_delete(tmp_path):
    book = ContactsBook(tmp_path / "contacts.json")
    row = book.save(name="Bob", emails=["old@example.com"])
    book.save(name="Bob", contact_id=row["id"], emails=["new@example.com"])

    assert book.resolve("Bob", "email").value == "new@example.com"
    assert book.delete(row["id"])
    assert book.list() == []

