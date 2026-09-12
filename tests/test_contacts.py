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



def test_update_merges_and_prefers_newest(tmp_path):
    book = ContactsBook(tmp_path / "contacts.json")
    row = book.save(name="Maman", emails=["maman@old.org"], phone="06 12 34 56 78")
    book.save(contact_id=row["id"], emails=["maman@new.org"], aliases=["mère"])
    updated = book.find("maman")[0]
    assert updated["emails"] == ["maman@new.org", "maman@old.org"]
    assert updated["aliases"] == ["mère"]
    assert updated["phone"] == "0612345678"
    assert book.resolve("mere", "email").value == "maman@new.org"


def test_fuzzy_find_tolerates_transcription(tmp_path):
    book = ContactsBook(tmp_path / "contacts.json")
    book.save(name="Abdoulaye Diallo")
    assert book.find("abdoulai")[0]["name"] == "Abdoulaye Diallo"
    assert book.find("zzzz") == []


def test_replace_field_removes_one_email(tmp_path):
    book = ContactsBook(tmp_path / "contacts.json")
    row = book.save(name="Paul", emails=["a@x.org", "b@x.org"])
    book.replace_field(row["id"], "emails", ["b@x.org"])
    assert book.find("paul")[0]["emails"] == ["b@x.org"]
