"""Offline importer checks: .venv/bin/python -m unittest test_papers_to_zotero -v."""

import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

import requests

import papers_to_zotero as importer


DOI = "10.1234/a"


def article(doi=DOI, published="2026-10-19"):
    return {"doi": doi, "published": published, "title": "Original title",
            "title_cn": "中文标题", "journal_name": "Test Journal",
            "authors": [{"given": "Jane", "family": "Doe"}], "abstract": "Abstract"}


def existing(key="ABCDEFGH"):
    return {"key": key, "data": {"DOI": DOI, "itemType": "journalArticle"}}


class SelectionTests(unittest.TestCase):
    def test_matches_viewer_window_and_deduplicates_normalized_dois(self):
        papers = [article(), article("https://doi.org/10.1234/A"),
                  article("10.1234/old", "2026-10-18"),
                  article("10.1234/future", "2026-10-27"),
                  article("10.1234/unstarred", "2026-10-25")]
        selected = importer.select_articles({"articles": papers},
                    {"starred": [DOI, "10.1234/old", "10.1234/future"]},
                    7, date(2026, 10, 26))
        self.assertEqual([p["doi"] for p in selected], [DOI])

    def test_explicit_doi_ignores_date_and_favorite_but_requires_archive_entry(self):
        archive = {"articles": [article(published="2020-01-01")]}
        self.assertEqual(len(importer.select_articles(archive, {}, 7,
                         date(2026, 10, 26), [DOI])), 1)
        with self.assertRaises(ValueError):
            importer.select_articles(archive, {}, 7, date(2026, 10, 26), ["10.1234/missing"])

    def test_metadata_keeps_original_title_and_authors(self):
        item = importer.zotero_item(article(), "connector-id")
        self.assertEqual(item["title"], "Original title")
        self.assertEqual(item["creators"][0]["lastName"], "Doe")
        self.assertEqual(item["DOI"], DOI)


class ImportTests(unittest.TestCase):
    def test_existing_pdf_does_not_write_or_download(self):
        client = MagicMock()
        client.find_items.return_value = [existing()]
        client.pdfs.return_value = [{"verified": True}]
        with patch.object(importer, "download_publisher_pdf") as download:
            result = importer.process_article(client, article(), True)
        self.assertEqual(result["status"], "existing_pdf")
        client.save.assert_not_called()
        client.resolve_pdf.assert_not_called()
        download.assert_not_called()

    def test_existing_missing_pdf_is_not_recreated(self):
        client = MagicMock()
        client.find_items.return_value = [existing()]
        client.pdfs.return_value = [{"verified": False}]
        result = importer.process_article(client, article(), True)
        self.assertEqual(result["status"], "existing_missing_pdf")
        client.save.assert_not_called()

    def test_duplicate_existing_items_are_reported_without_writes(self):
        client = MagicMock()
        client.find_items.return_value = [existing(), existing("IJKLMNOP")]
        result = importer.process_article(client, article(), True)
        self.assertEqual(result["status"], "existing_duplicates")
        client.save.assert_not_called()

    def test_check_mode_never_saves_missing_article(self):
        client = MagicMock()
        client.find_items.return_value = []
        result = importer.process_article(client, article(), False)
        self.assertEqual(result["status"], "would_import")
        client.save.assert_not_called()

    def test_new_import_explicitly_resolves_and_verifies_pdf(self):
        client = MagicMock()
        client.find_items.side_effect = [[], [existing()]]
        client.save.return_value = ("session", "connector")
        client.pdfs.return_value = [{"verified": True}]
        with patch.object(importer, "download_publisher_pdf") as download:
            result = importer.process_article(client, article(), True)
        self.assertEqual(result["status"], "imported_pdf")
        client.save.assert_called_once()
        client.resolve_pdf.assert_called_once_with("session", "connector")
        download.assert_not_called()

    def test_resolver_failure_falls_back_to_publisher_and_upload(self):
        client = MagicMock()
        client.find_items.side_effect = [[], [existing()], [existing()]]
        client.save.return_value = ("session", "connector")
        client.resolve_pdf.side_effect = requests.HTTPError("No OA file")
        client.pdfs.side_effect = [[], [{"verified": True}]]
        with patch.object(importer, "download_publisher_pdf",
                          return_value=("https://publisher.test/a.pdf", b"%PDF-test")):
            result = importer.process_article(client, article(), True)
        client.upload_pdf.assert_called_once_with("session", "connector",
                                 "https://publisher.test/a.pdf", b"%PDF-test")
        self.assertEqual(result["status"], "imported_pdf")

    def test_metadata_only_is_failure_not_pdf_success(self):
        client = MagicMock()
        client.find_items.side_effect = [[], [existing()], [existing()]]
        client.save.return_value = ("session", "connector")
        client.resolve_pdf.side_effect = requests.HTTPError("No OA file")
        client.pdfs.return_value = []
        with patch.object(importer, "download_publisher_pdf",
                          side_effect=requests.HTTPError("403 CAPTCHA")):
            result = importer.process_article(client, article(), True)
        self.assertEqual(result["status"], "imported_missing_pdf")
        client.save.assert_called_once()

    def test_ambiguous_timeout_is_never_retried(self):
        client = MagicMock()
        client.find_items.return_value = []
        client.save.side_effect = requests.Timeout("Write may have succeeded")
        with self.assertRaises(requests.Timeout):
            importer.process_article(client, article(), True)
        client.save.assert_called_once()
        client.resolve_pdf.assert_not_called()


class LocalAPITests(unittest.TestCase):
    def test_pdf_verification_rejects_html_disguised_as_pdf(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "file.pdf"
            path.write_bytes(b"<html>Access denied</html>")
            response = MagicMock()
            response.json.return_value = [{"key": "PDFKEY01",
                "data": {"contentType": "application/pdf"},
                "links": {"enclosure": {"href": path.as_uri()}}}]
            client = importer.ZoteroClient()
            with patch.object(client, "request", return_value=response):
                self.assertFalse(client.pdfs(existing())[0]["verified"])
                path.write_bytes(b"%PDF-1.7\ncontents")
                self.assertTrue(client.pdfs(existing())[0]["verified"])

    def test_remote_api_origin_rejected(self):
        for url in ["https://api.zotero.org", "http://example.com:23119",
                    "http://localhost:23119/api", "http://name:secret@localhost:23119"]:
            with self.subTest(url=url), self.assertRaises(ValueError):
                importer.ZoteroClient(url)

    def test_connector_v3_metadata_then_binary_attachment_contract(self):
        client = importer.ZoteroClient()
        with patch.object(client, "request") as request:
            session, connector = client.save(article())
            payload = request.call_args.kwargs["json"]
            self.assertEqual(payload["sessionID"], session)
            self.assertEqual(payload["items"][0]["id"], connector)
            self.assertEqual(payload["items"][0]["attachments"], [])
            client.upload_pdf(session, connector, "https://publisher.test/a.pdf", b"%PDF-test")
            headers = request.call_args.kwargs["headers"]
            self.assertEqual(json.loads(headers["X-Metadata"])["parentItemID"], connector)
            self.assertEqual(request.call_args.kwargs["data"], b"%PDF-test")
            with self.assertRaises(ValueError):
                client.upload_pdf(session, connector, "https://publisher.test/a.pdf", b"<html>")

    def test_group_save_target_rejected_before_saving(self):
        client = importer.ZoteroClient()
        response = MagicMock()
        response.json.return_value = {"libraryID": 9, "filesEditable": True}
        with patch.object(client, "request", return_value=response):
            with self.assertRaises(RuntimeError):
                client.check(True)


if __name__ == "__main__":
    unittest.main()
