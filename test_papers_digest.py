"""Regression checks: .venv/bin/python -m unittest test_papers_digest -v."""

import argparse
import json
import tempfile
import threading
import unittest
from datetime import date
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import papers_digest as pd
import papers_server as server


def paper(doi="10.test/one", published="2026-09-01", **fields):
    value = dict(doi=doi, title="A paper", journal="JCTC", journal_name="Journal",
                 abstract="Abstract", abstract_cn="摘要", title_cn="论文", authors=[],
                 volume="", issue="", page="", published=published, url="https://doi.org/" + doi)
    value.update(fields)
    return value


class HistoryTests(unittest.TestCase):
    def test_window_expiry_keeps_old_and_starred_papers(self):
        old = paper(first_seen_at="2026-09-01T08:00:00+08:00")
        merged, added = pd.merge_archive({"articles": [old]}, [paper("10.test/new")],
                                        "2026-10-08T10:00:00+08:00")
        self.assertEqual({a["doi"] for a in merged}, {"10.test/one", "10.test/new"})
        self.assertEqual(added, 1)
        self.assertEqual(next(a for a in merged if a["doi"] == old["doi"])["first_seen_at"],
                         old["first_seen_at"])

    def test_metadata_gap_does_not_erase_abstract_or_translation(self):
        old = paper(first_seen_at="2026-09-01T08:00:00+08:00")
        merged, added = pd.merge_archive({"articles": [old]},
                                        [paper(abstract="", abstract_cn="", title_cn="")],
                                        "2026-10-08T10:00:00+08:00")
        self.assertEqual(merged[0]["abstract"], old["abstract"])
        self.assertEqual(merged[0]["abstract_cn"], old["abstract_cn"])
        self.assertEqual(added, 0)

    def test_covers_filtered_during_history_merge(self):
        merged, added = pd.merge_archive({"articles": [paper(title="Contents list")]},
                                        [paper(title="Front cover")], "2026-10-08T00:00:00Z")
        self.assertEqual(merged, [])
        self.assertEqual(added, 0)

    def test_changed_original_does_not_reuse_stale_translation(self):
        merged, _ = pd.merge_archive({"articles": [paper()]},
                                     [paper(title="Corrected title", abstract="Corrected abstract",
                                            title_cn="", abstract_cn="")], "2026-10-08T00:00:00Z")
        self.assertEqual(merged[0]["title_cn"], "")
        self.assertEqual(merged[0]["abstract_cn"], "")

    def test_all_fetch_failures_preserve_page_and_archive(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            page = root / "papers-digest.html"
            archive = root / "papers-archive.json"
            page.write_text("last good page")
            archive.write_text("last good archive")
            args = argparse.Namespace(days=30, fetch_workers=4)
            def fail(_journals, _days, workers, failures):
                failures.append("JCTC")
                return []
            with patch.object(pd, "ROOT", root), patch.object(pd, "UPDATE_FILE", root / "status.json"), \
                 patch.object(pd, "parse_args", return_value=args), \
                 patch.object(pd, "load_journals", return_value=[{"short": "JCTC"}]), \
                 patch.object(pd, "collect_articles", side_effect=fail):
                with self.assertRaises(SystemExit) as error:
                    pd.main()
            self.assertEqual(error.exception.code, 1)
            self.assertEqual(page.read_text(), "last good page")
            self.assertEqual(archive.read_text(), "last good archive")
            self.assertEqual(json.loads((root / "status.json").read_text())["state"], "failed")

    def test_snapshot_migration_skips_empty_days_and_preserves_first_seen(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            article = {"doi": "10.test/one", "journal": "JCTC", "title": "Paper", "titleCn": "论文",
                       "abstract": "abstract", "published": "2026-09-01"}
            def snapshot(day, entries):
                (root / f"papers-digest-{day}.html").write_text(
                    f'<p>生成于 {day} 10:00</p><script id="article-data" type="application/json">' +
                    json.dumps(entries) + "</script>")
            snapshot("2026-09-30", [article])
            snapshot("2026-10-01", [])
            snapshot("2026-10-08", [article])
            with patch.object(pd, "ROOT", root), patch.object(pd, "ARCHIVE_FILE", root / "archive.json"):
                archive = pd.load_archive()
            self.assertEqual(len(archive["articles"]), 1)
            self.assertTrue(archive["articles"][0]["first_seen_at"].startswith("2026-09-30"))
            self.assertTrue(archive["lastSuccessAt"].startswith("2026-10-08"))

    def test_partial_failure_keeps_failed_journal_and_last_success_time(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            previous_success = "2026-10-07T10:00:00+08:00"
            archive_file = root / "archive.json"
            archive_file.write_text(json.dumps({"schema": 1, "lastSuccessAt": previous_success,
                                               "articles": [paper("10.test/old", journal="JCP",
                                                                  first_seen_at=previous_success)]}))
            args = argparse.Namespace(days=30, fetch_workers=4, limit=None, no_llm=True,
                                      cache_dir=root / "cache", output=None)
            def partial(_journals, _days, workers, failures):
                failures.append("JCP")
                return [paper("10.test/new")]
            with patch.object(pd, "ROOT", root), patch.object(pd, "ARCHIVE_FILE", archive_file), \
                 patch.object(pd, "UPDATE_FILE", root / "status.json"), \
                 patch.object(pd, "parse_args", return_value=args), \
                 patch.object(pd, "load_journals", return_value=[{"short": "JCP"}, {"short": "JCTC"}]), \
                 patch.object(pd, "collect_articles", side_effect=partial), \
                 patch.object(pd, "fill_missing_abstracts", return_value=0), \
                 patch.object(pd, "apply_cached_translations", return_value=0), \
                 patch.object(pd, "render_html"):
                pd.main()
            stored = json.loads(archive_file.read_text())
            status = json.loads((root / "status.json").read_text())
            self.assertEqual({a["doi"] for a in stored["articles"]}, {"10.test/old", "10.test/new"})
            self.assertEqual(stored["lastSuccessAt"], previous_success)
            self.assertEqual(status["state"], "partial")
            self.assertEqual(status["failedJournals"], ["JCP"])


class DateRangeTests(unittest.TestCase):
    def test_invalid_ranges_are_rejected_before_fetching(self):
        for start, end in (("2025-01-01", None), ("2025-02-30", "2025-03-01"),
                           ("2025-02-01", "2025-01-31"), ("2025-01-01", "2999-01-01"),
                           ("20250101", "2025-01-31"), ([], "2025-01-31")):
            with self.subTest(start=start, end=end), self.assertRaises(ValueError):
                pd.resolve_date_range(start, end)
        self.assertEqual(pd.resolve_date_range("2025-01-01", "2025-01-01"),
                         (date(2025, 1, 1), date(2025, 1, 1)))

    def test_historical_queries_cover_rsc_and_late_registered_dois(self):
        journal = {"short": "JCTC", "name": "Journal", "issn": "1234-5678"}
        def item(doi, online, created):
            return {"DOI": doi, "title": ["Paper"], "published-online": {"date-parts": [online]},
                    "created": {"date-parts": [created]}}
        first = item("10.test/start", [2025, 1, 1], [2024, 12, 15])
        rsc = item("10.test/rsc", [2025], [2025, 1, 31])
        late = item("10.test/late", [2025, 1, 15], [2025, 5, 1])
        outside = item("10.test/outside", [2025, 2, 1], [2025, 1, 31])
        # 第一查询翻页；第二查询重复同一 DOI 并补上晚注册论文。
        responses = [{"message": {"items": [first, outside], "next-cursor": "page2"}},
                     {"message": {"items": [rsc]}},
                     {"message": {"items": [first, late]}}]
        with patch.object(pd, "request_json", side_effect=responses) as request, \
             patch.object(pd.time, "sleep"):
            articles = pd.fetch_journal(journal, 30, date(2026, 10, 12),
                                        date(2025, 1, 1), date(2025, 1, 31))
        self.assertEqual({a["doi"] for a in articles},
                         {"10.test/start", "10.test/rsc", "10.test/late"})
        self.assertIn("until-created-date:2025-03-02", request.call_args_list[0].args[1]["filter"])
        self.assertIn("from-pub-date:2025-01-01", request.call_args_list[-1].args[1]["filter"])
        self.assertEqual(request.call_args_list[1].args[1]["cursor"], "page2")

    def test_historical_update_merges_history_and_records_selected_range(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            archive_file = root / "archive.json"
            archive_file.write_text(json.dumps({"schema": 1, "articles": [paper()]}))
            args = argparse.Namespace(days=30, start_date="2025-01-01", end_date="2025-01-31",
                                      fetch_workers=4, limit=None, no_llm=True,
                                      cache_dir=root / "cache", output=None)
            with patch.object(pd, "ROOT", root), patch.object(pd, "ARCHIVE_FILE", archive_file), \
                 patch.object(pd, "UPDATE_FILE", root / "status.json"), \
                 patch.object(pd, "parse_args", return_value=args), \
                 patch.object(pd, "load_journals", return_value=[{"short": "JCTC"}]), \
                 patch.object(pd, "collect_articles", return_value=[paper("10.test/old", "2025-01-15")]) as collect, \
                 patch.object(pd, "apply_cached_translations", return_value=0), \
                 patch.object(pd, "render_html"):
                pd.main()
            self.assertEqual(collect.call_args.kwargs["start_date"], date(2025, 1, 1))
            self.assertEqual(collect.call_args.kwargs["end_date"], date(2025, 1, 31))
            self.assertEqual({a["doi"] for a in json.loads(archive_file.read_text())["articles"]},
                             {"10.test/one", "10.test/old"})
            self.assertEqual(json.loads((root / "status.json").read_text())["endDate"], "2025-01-31")


class ReadingTests(unittest.TestCase):
    def test_out_of_order_updates_and_other_papers_survive(self):
        with tempfile.TemporaryDirectory() as folder, \
             patch.object(server, "READING_FILE", Path(folder) / "reading.json"):
            server.save_reading({"a": {"status": "full", "updatedAt": "2026-10-08T12:00:00+08:00"},
                                "b": {"status": "browsed", "updatedAt": "2026-10-08T12:00:00+08:00"}})
            server.save_reading({"a": {"status": "browsed", "updatedAt": "2026-10-08T02:00:00Z"}})
            self.assertEqual(server.load_reading()["a"]["status"], "full")
            self.assertEqual(server.load_reading()["b"]["status"], "browsed")
            server.save_reading({"a": {"status": "unread", "updatedAt": "2026-10-08T13:00:00+08:00"}})
            self.assertEqual(server.load_reading()["a"]["status"], "unread")

    def test_invalid_updates_do_not_replace_records(self):
        with tempfile.TemporaryDirectory() as folder, \
             patch.object(server, "READING_FILE", Path(folder) / "reading.json"):
            server.save_reading({"a": {"status": "full", "updatedAt": "2026-10-08T00:00:00Z"}})
            with self.assertRaises(ValueError):
                server.save_reading({"a": {"status": "invented", "updatedAt": "2026-10-08T00:00:00Z"}})
            self.assertEqual(server.load_reading()["a"]["status"], "full")


class TranslationTests(unittest.TestCase):
    def test_update_prompts_never_include_abstracts_even_in_fallback(self):
        with tempfile.TemporaryDirectory() as folder:
            articles = [paper("10.test/one", title="First title", title_cn="", abstract_cn="",
                              abstract="SECRET_ABSTRACT_ONE"),
                        paper("10.test/two", title="Second title", title_cn="", abstract_cn="",
                              abstract="SECRET_ABSTRACT_TWO")]
            llm = {"model": "test"}
            with patch.object(pd, "call_llm", side_effect=["【1】\n标题: 第一篇", "## 标题\n第二篇"]) as call:
                cached, fresh = pd.translate_all(articles, llm, Path(folder), workers=1)
            self.assertEqual((cached, fresh), (0, 2))
            self.assertEqual(call.call_count, 2)
            for request in call.call_args_list:
                self.assertNotIn("SECRET_ABSTRACT", request.args[0])
            self.assertTrue(all(a["title_cn"] for a in articles))
            self.assertTrue(all(not a["abstract_cn"] for a in articles))

    def test_title_cache_is_independent_of_abstract_changes(self):
        with tempfile.TemporaryDirectory() as folder:
            cache = Path(folder)
            pd.write_cache(cache, paper())
            changed = paper(abstract="Updated abstract", title_cn="", abstract_cn="")
            self.assertTrue(pd.apply_cache(changed, pd.read_cache(cache)))
            self.assertEqual(changed["title_cn"], "论文")
            self.assertEqual(changed["abstract_cn"], "")

    def test_old_cache_is_reused_and_title_update_preserves_abstract(self):
        with tempfile.TemporaryDirectory() as folder:
            cache = Path(folder)
            article = paper()
            target = cache / pd.doi_cache_name(article["doi"])
            target.write_text(json.dumps({"schema": 2, "doi": article["doi"],
                                          "en_hash": pd.content_hash(article),
                                          "title_cn": "原有标题", "abstract_cn": "原有摘要"}))
            fresh = paper(title_cn="", abstract_cn="")
            self.assertTrue(pd.apply_cache(fresh, pd.read_cache(cache)))
            self.assertEqual(fresh["abstract_cn"], "原有摘要")
            pd.write_cache(cache, fresh, title_only=True)
            entry = pd.read_cache_entry(cache, article["doi"])
            self.assertEqual(entry["schema"], 3)
            self.assertEqual(entry["abstract_cn"], "原有摘要")
            pd.write_cache(cache, paper(title_cn="改进标题", abstract_cn=""), title_only=True)
            self.assertEqual(pd.read_cache_entry(cache, article["doi"])["abstract_cn"], "原有摘要")

    def test_abstract_only_write_does_not_overwrite_new_title(self):
        with tempfile.TemporaryDirectory() as folder:
            cache = Path(folder)
            pd.write_cache(cache, paper(title="New title", title_cn="新标题"), title_only=True)
            pd.write_cache(cache, paper(title="Old title", title_cn="旧标题"), abstract_only=True)
            self.assertEqual(pd.read_cache_entry(cache, "10.test/one")["title_cn"], "新标题")

    def test_on_demand_translation_is_persisted_and_called_only_once(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            archive = root / "archive.json"
            archive.write_text(json.dumps({"schema": 1, "articles": [paper(abstract_cn="")]}))
            with patch.object(pd, "ARCHIVE_FILE", archive), patch.object(pd, "CACHE_DIR", root / "cache"), \
                 patch.object(pd, "get_llm_config", return_value={"model": "test"}), \
                 patch.object(pd, "translate_abstract", return_value="按需译文") as translate:
                first = server.translate_one("10.test/one")
                second = server.translate_one("10.test/one")
            self.assertEqual(translate.call_count, 1)
            self.assertFalse(first["cached"])
            self.assertTrue(second["cached"])
            self.assertEqual(json.loads(archive.read_text())["articles"][0]["abstract_cn"], "按需译文")

    def test_missing_abstract_uses_openalex_only_when_requested(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            archive = root / "archive.json"
            archive.write_text(json.dumps({"schema": 1, "articles": [paper(abstract="", abstract_cn="")]}))
            with patch.object(pd, "ARCHIVE_FILE", archive), patch.object(pd, "CACHE_DIR", root / "cache"), \
                 patch.object(pd, "fetch_openalex_abstract", return_value="OpenAlex abstract") as fetch, \
                 patch.object(pd, "get_llm_config", return_value={"model": "test"}), \
                 patch.object(pd, "translate_abstract", return_value="补齐的摘要"):
                result = server.translate_one("10.test/one")
                server.translate_one("10.test/one")
            self.assertEqual(fetch.call_count, 1)
            self.assertEqual(result["abstractSource"], "openalex")


class ManualUpdateTests(unittest.TestCase):
    def test_other_websites_cannot_trigger_updates(self):
        http = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        thread = threading.Thread(target=http.serve_forever, daemon=True)
        thread.start()
        url = f"http://127.0.0.1:{http.server_port}/api/update"
        try:
            with patch.object(server, "start_manual_update", return_value={"running": False}) as start:
                for origin in ("https://example.com", "http://localhost:invalid"):
                    with self.assertRaises(HTTPError) as error:
                        urlopen(Request(url, data=b"{}", headers={"Origin": origin}))
                    self.assertEqual(error.exception.code, 403)
                start.assert_not_called()
                response = urlopen(Request(url, data=b"{}", headers={"Origin": f"http://127.0.0.1:{http.server_port}"}))
                self.assertEqual(response.status, 202)
                start.assert_called_once_with({})
        finally:
            http.shutdown()
            http.server_close()
            thread.join()

    def test_repeated_clicks_start_only_one_job(self):
        with patch.dict(server._update_state, {"running": False}, clear=True), \
             patch.object(server.threading, "Thread") as thread:
            self.assertTrue(server.start_manual_update()["started"])
            self.assertFalse(server.start_manual_update()["started"])
            self.assertEqual(thread.call_count, 1)

    def test_custom_update_keeps_the_original_range_on_repeated_click(self):
        with patch.dict(server._update_state, {"running": False}, clear=True), \
             patch.object(server.threading, "Thread") as thread:
            job = server.start_manual_update({"startDate": "2025-01-01", "endDate": "2025-01-31"})
            repeated = server.start_manual_update({"startDate": "2025-02-01", "endDate": "2025-02-28"})
            self.assertTrue(job["started"])
            self.assertFalse(repeated["started"])
            self.assertEqual(repeated["startDate"], "2025-01-01")
            self.assertEqual(thread.call_args.kwargs["args"],
                             ({"startDate": "2025-01-01", "endDate": "2025-01-31"},))

    def test_invalid_api_dates_return_400_and_do_not_start_job(self):
        http = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        thread = threading.Thread(target=http.serve_forever, daemon=True)
        thread.start()
        try:
            with patch.dict(server._update_state, {"running": False}, clear=True):
                with self.assertRaises(HTTPError) as error:
                    urlopen(Request(f"http://127.0.0.1:{http.server_port}/api/update",
                                    data=b'{"startDate":"2025-02-01","endDate":"2025-01-01"}'))
                self.assertEqual(error.exception.code, 400)
                self.assertIn("开始日期", json.loads(error.exception.read())["error"])
                self.assertFalse(server.get_update_state()["running"])
        finally:
            http.shutdown()
            http.server_close()
            thread.join()

    def test_subprocess_receives_historical_range_and_two_translation_workers(self):
        process = MagicMock()
        process.stdout = iter([])
        process.wait.return_value = 0
        context = MagicMock()
        context.__enter__.return_value = process
        with patch.object(server.subprocess, "Popen", return_value=context) as popen, \
             patch.object(pd, "read_json_file", return_value={"state": "ok"}), \
             patch.dict(server._update_state, {"running": True}, clear=True):
            server.run_manual_update({"startDate": "2025-01-01", "endDate": "2025-01-31"})
            command = popen.call_args.args[0]
            self.assertEqual(command[command.index("--workers") + 1], "2")
            self.assertEqual(command[-4:], ["--start-date", "2025-01-01", "--end-date", "2025-01-31"])

    def test_failure_keeps_page_and_reports_failure_after_reload(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            page = root / "papers-digest.html"
            page.write_text("last good page")
            status = root / "status.json"
            status.write_text(json.dumps({"state": "ok", "lastSuccessAt": "2026-10-08T10:05:00+08:00"}))
            process = MagicMock()
            process.stdout = iter(["connection failed\n"])
            process.wait.return_value = 1
            context = MagicMock()
            context.__enter__.return_value = process
            with patch.object(server, "ROOT", root), patch.object(pd, "UPDATE_FILE", status), \
                 patch.dict(server._update_state, {"running": True}, clear=True), \
                 patch.object(server.subprocess, "Popen", return_value=context) as popen:
                server.run_manual_update()
                result = server.get_update_state()
            self.assertFalse(result["running"])
            self.assertFalse(result["succeeded"])
            self.assertEqual(json.loads(status.read_text())["state"], "failed")
            self.assertEqual(page.read_text(), "last good page")
            self.assertEqual(popen.call_args.args[0][-2:], ["--workers", "2"])


if __name__ == "__main__":
    unittest.main()
