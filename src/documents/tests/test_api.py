import datetime
import io
import json
import os
import shutil
import tempfile
import urllib.request
import uuid
import zipfile
from datetime import timedelta
from pathlib import Path
from unittest import mock
from unittest.mock import MagicMock

import celery

try:
    import zoneinfo
except ImportError:
    import backports.zoneinfo as zoneinfo

import pytest
from django.conf import settings
from django.contrib.auth.models import Group
from django.contrib.auth.models import Permission
from django.contrib.auth.models import User
from django.test import override_settings
from django.utils import timezone
from dateutil.relativedelta import relativedelta
from rest_framework import status
from documents import bulk_edit
from documents import index
from documents.models import Correspondent
from documents.models import Document
from documents.tests.utils import DocumentConsumeDelayMixin
from documents.models import DocumentType
from documents.models import MatchingModel
from documents.models import PaperlessTask
from documents.models import SavedView
from documents.models import StoragePath
from documents.models import Tag
from documents.models import Comment
from documents.tests.utils import DirectoriesMixin
from paperless import version
from rest_framework.test import APITestCase
from whoosh.writing import AsyncWriter


class TestDocumentApi(DirectoriesMixin, DocumentConsumeDelayMixin, APITestCase):
    def setUp(self):
        super().setUp()

        self.user = User.objects.create_superuser(username="temp_admin")
        self.client.force_authenticate(user=self.user)

    def testDocuments(self):

        response = self.client.get("/api/documents/").data

        self.assertEqual(response["count"], 0)

        c = Correspondent.objects.create(name="c", pk=41)
        dt = DocumentType.objects.create(name="dt", pk=63)
        tag = Tag.objects.create(name="t", pk=85)

        doc = Document.objects.create(
            title="WOW",
            content="the content",
            correspondent=c,
            document_type=dt,
            checksum="123",
            mime_type="application/pdf",
        )

        doc.tags.add(tag)

        response = self.client.get("/api/documents/", format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["count"], 1)

        returned_doc = response.data["results"][0]
        self.assertEqual(returned_doc["id"], doc.id)
        self.assertEqual(returned_doc["title"], doc.title)
        self.assertEqual(returned_doc["correspondent"], c.id)
        self.assertEqual(returned_doc["document_type"], dt.id)
        self.assertListEqual(returned_doc["tags"], [tag.id])

        c2 = Correspondent.objects.create(name="c2")

        returned_doc["correspondent"] = c2.pk
        returned_doc["title"] = "the new title"

        response = self.client.put(
            f"/api/documents/{doc.pk}/",
            returned_doc,
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)

        doc_after_save = Document.objects.get(id=doc.id)

        self.assertEqual(doc_after_save.correspondent, c2)
        self.assertEqual(doc_after_save.title, "the new title")

        self.client.delete(f"/api/documents/{doc_after_save.pk}/")

        self.assertEqual(len(Document.objects.all()), 0)

    def test_document_fields(self):
        c = Correspondent.objects.create(name="c", pk=41)
        dt = DocumentType.objects.create(name="dt", pk=63)
        tag = Tag.objects.create(name="t", pk=85)
        storage_path = StoragePath.objects.create(name="sp", pk=77, path="p")
        doc = Document.objects.create(
            title="WOW",
            content="the content",
            correspondent=c,
            document_type=dt,
            checksum="123",
            mime_type="application/pdf",
            storage_path=storage_path,
        )

        response = self.client.get("/api/documents/", format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        results_full = response.data["results"]
        self.assertIn("content", results_full[0])
        self.assertIn("id", results_full[0])

        response = self.client.get("/api/documents/?fields=id", format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        results = response.data["results"]
        self.assertFalse("content" in results[0])
        self.assertIn("id", results[0])
        self.assertEqual(len(results[0]), 1)

        response = self.client.get("/api/documents/?fields=content", format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        results = response.data["results"]
        self.assertIn("content", results[0])
        self.assertFalse("id" in results[0])
        self.assertEqual(len(results[0]), 1)

        response = self.client.get("/api/documents/?fields=id,content", format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        results = response.data["results"]
        self.assertIn("content", results[0])
        self.assertIn("id", results[0])
        self.assertEqual(len(results[0]), 2)

        response = self.client.get(
            "/api/documents/?fields=id,conteasdnt",
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        results = response.data["results"]
        self.assertFalse("content" in results[0])
        self.assertIn("id", results[0])
        self.assertEqual(len(results[0]), 1)

        response = self.client.get("/api/documents/?fields=", format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        results = response.data["results"]
        self.assertEqual(len(results_full[0]), len(results[0]))

        response = self.client.get("/api/documents/?fields=dgfhs", format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        results = response.data["results"]
        self.assertEqual(len(results[0]), 0)

    def test_document_actions(self):

        _, filename = tempfile.mkstemp(dir=self.dirs.originals_dir)

        content = b"This is a test"
        content_thumbnail = b"thumbnail content"

        with open(filename, "wb") as f:
            f.write(content)

        doc = Document.objects.create(
            title="none",
            filename=os.path.basename(filename),
            mime_type="application/pdf",
        )

        with open(
            os.path.join(self.dirs.thumbnail_dir, f"{doc.pk:07d}.webp"),
            "wb",
        ) as f:
            f.write(content_thumbnail)

        response = self.client.get(f"/api/documents/{doc.pk}/download/")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.content, content)

        response = self.client.get(f"/api/documents/{doc.pk}/preview/")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.content, content)

        response = self.client.get(f"/api/documents/{doc.pk}/thumb/")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.content, content_thumbnail)

    @override_settings(FILENAME_FORMAT="")
    def test_download_with_archive(self):

        content = b"This is a test"
        content_archive = b"This is the same test but archived"

        doc = Document.objects.create(
            title="none",
            filename="my_document.pdf",
            archive_filename="archived.pdf",
            mime_type="application/pdf",
        )

        with open(doc.source_path, "wb") as f:
            f.write(content)

        with open(doc.archive_path, "wb") as f:
            f.write(content_archive)

        response = self.client.get(f"/api/documents/{doc.pk}/download/")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.content, content_archive)

        response = self.client.get(
            f"/api/documents/{doc.pk}/download/?original=true",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.content, content)

        response = self.client.get(f"/api/documents/{doc.pk}/preview/")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.content, content_archive)

        response = self.client.get(
            f"/api/documents/{doc.pk}/preview/?original=true",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.content, content)

    def test_document_actions_not_existing_file(self):

        doc = Document.objects.create(
            title="none",
            filename=os.path.basename("asd"),
            mime_type="application/pdf",
        )

        response = self.client.get(f"/api/documents/{doc.pk}/download/")
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

        response = self.client.get(f"/api/documents/{doc.pk}/preview/")
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

        response = self.client.get(f"/api/documents/{doc.pk}/thumb/")
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_document_filters(self):

        doc1 = Document.objects.create(
            title="none1",
            checksum="A",
            mime_type="application/pdf",
        )
        doc2 = Document.objects.create(
            title="none2",
            checksum="B",
            mime_type="application/pdf",
        )
        doc3 = Document.objects.create(
            title="none3",
            checksum="C",
            mime_type="application/pdf",
        )

        tag_inbox = Tag.objects.create(name="t1", is_inbox_tag=True)
        tag_2 = Tag.objects.create(name="t2")
        tag_3 = Tag.objects.create(name="t3")

        doc1.tags.add(tag_inbox)
        doc2.tags.add(tag_2)
        doc3.tags.add(tag_2)
        doc3.tags.add(tag_3)

        response = self.client.get("/api/documents/?is_in_inbox=true")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        results = response.data["results"]
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["id"], doc1.id)

        response = self.client.get("/api/documents/?is_in_inbox=false")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        results = response.data["results"]
        self.assertEqual(len(results), 2)
        self.assertCountEqual([results[0]["id"], results[1]["id"]], [doc2.id, doc3.id])

        response = self.client.get(
            f"/api/documents/?tags__id__in={tag_inbox.id},{tag_3.id}",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        results = response.data["results"]
        self.assertEqual(len(results), 2)
        self.assertCountEqual([results[0]["id"], results[1]["id"]], [doc1.id, doc3.id])

        response = self.client.get(
            f"/api/documents/?tags__id__in={tag_2.id},{tag_3.id}",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        results = response.data["results"]
        self.assertEqual(len(results), 2)
        self.assertCountEqual([results[0]["id"], results[1]["id"]], [doc2.id, doc3.id])

        response = self.client.get(
            f"/api/documents/?tags__id__all={tag_2.id},{tag_3.id}",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        results = response.data["results"]
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["id"], doc3.id)

        response = self.client.get(
            f"/api/documents/?tags__id__all={tag_inbox.id},{tag_3.id}",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        results = response.data["results"]
        self.assertEqual(len(results), 0)

        response = self.client.get(
            f"/api/documents/?tags__id__all={tag_inbox.id}a{tag_3.id}",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        results = response.data["results"]
        self.assertEqual(len(results), 3)

        response = self.client.get(f"/api/documents/?tags__id__none={tag_3.id}")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        results = response.data["results"]
        self.assertEqual(len(results), 2)
        self.assertCountEqual([results[0]["id"], results[1]["id"]], [doc1.id, doc2.id])

        response = self.client.get(
            f"/api/documents/?tags__id__none={tag_3.id},{tag_2.id}",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        results = response.data["results"]
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["id"], doc1.id)

        response = self.client.get(
            f"/api/documents/?tags__id__none={tag_2.id},{tag_inbox.id}",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        results = response.data["results"]
        self.assertEqual(len(results), 0)

    def test_documents_title_content_filter(self):

        doc1 = Document.objects.create(
            title="title A",
            content="content A",
            checksum="A",
            mime_type="application/pdf",
        )
        doc2 = Document.objects.create(
            title="title B",
            content="content A",
            checksum="B",
            mime_type="application/pdf",
        )
        doc3 = Document.objects.create(
            title="title A",
            content="content B",
            checksum="C",
            mime_type="application/pdf",
        )
        doc4 = Document.objects.create(
            title="title B",
            content="content B",
            checksum="D",
            mime_type="application/pdf",
        )

        response = self.client.get("/api/documents/?title_content=A")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        results = response.data["results"]
        self.assertEqual(len(results), 3)
        self.assertCountEqual(
            [results[0]["id"], results[1]["id"], results[2]["id"]],
            [doc1.id, doc2.id, doc3.id],
        )

        response = self.client.get("/api/documents/?title_content=B")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        results = response.data["results"]
        self.assertEqual(len(results), 3)
        self.assertCountEqual(
            [results[0]["id"], results[1]["id"], results[2]["id"]],
            [doc2.id, doc3.id, doc4.id],
        )

        response = self.client.get("/api/documents/?title_content=X")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        results = response.data["results"]
        self.assertEqual(len(results), 0)

    def test_search(self):
        d1 = Document.objects.create(
            title="invoice",
            content="the thing i bought at a shop and paid with bank account",
            checksum="A",
            pk=1,
        )
        d2 = Document.objects.create(
            title="bank statement 1",
            content="things i paid for in august",
            pk=2,
            checksum="B",
        )
        d3 = Document.objects.create(
            title="bank statement 3",
            content="things i paid for in september",
            pk=3,
            checksum="C",
        )
        with AsyncWriter(index.open_index()) as writer:
            # Note to future self: there is a reason we dont use a model signal handler to update the index: some operations edit many documents at once
            # (retagger, renamer) and we don't want to open a writer for each of these, but rather perform the entire operation with one writer.
            # That's why we cant open the writer in a model on_save handler or something.
            index.update_document(writer, d1)
            index.update_document(writer, d2)
            index.update_document(writer, d3)
        response = self.client.get("/api/documents/?query=bank")
        results = response.data["results"]
        self.assertEqual(response.data["count"], 3)
        self.assertEqual(len(results), 3)

        response = self.client.get("/api/documents/?query=september")
        results = response.data["results"]
        self.assertEqual(response.data["count"], 1)
        self.assertEqual(len(results), 1)

        response = self.client.get("/api/documents/?query=statement")
        results = response.data["results"]
        self.assertEqual(response.data["count"], 2)
        self.assertEqual(len(results), 2)

        response = self.client.get("/api/documents/?query=sfegdfg")
        results = response.data["results"]
        self.assertEqual(response.data["count"], 0)
        self.assertEqual(len(results), 0)

    def test_search_multi_page(self):
        with AsyncWriter(index.open_index()) as writer:
            for i in range(55):
                doc = Document.objects.create(
                    checksum=str(i),
                    pk=i + 1,
                    title=f"Document {i+1}",
                    content="content",
                )
                index.update_document(writer, doc)

        # This is here so that we test that no document gets returned twice (might happen if the paging is not working)
        seen_ids = []

        for i in range(1, 6):
            response = self.client.get(
                f"/api/documents/?query=content&page={i}&page_size=10",
            )
            results = response.data["results"]
            self.assertEqual(response.data["count"], 55)
            self.assertEqual(len(results), 10)

            for result in results:
                self.assertNotIn(result["id"], seen_ids)
                seen_ids.append(result["id"])

        response = self.client.get("/api/documents/?query=content&page=6&page_size=10")
        results = response.data["results"]
        self.assertEqual(response.data["count"], 55)
        self.assertEqual(len(results), 5)

        for result in results:
            self.assertNotIn(result["id"], seen_ids)
            seen_ids.append(result["id"])

    def test_search_invalid_page(self):
        with AsyncWriter(index.open_index()) as writer:
            for i in range(15):
                doc = Document.objects.create(
                    checksum=str(i),
                    pk=i + 1,
                    title=f"Document {i+1}",
                    content="content",
                )
                index.update_document(writer, doc)

        response = self.client.get("/api/documents/?query=content&page=0&page_size=10")
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        response = self.client.get("/api/documents/?query=content&page=3&page_size=10")
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    @override_settings(
        TIME_ZONE="UTC",
    )
    def test_search_added_in_last_week(self):
        """
        GIVEN:
            - Three documents added right now
            - The timezone is UTC time
        WHEN:
            - Query for documents added in the last 7 days
        THEN:
            - All three recent documents are returned
        """
        d1 = Document.objects.create(
            title="invoice",
            content="the thing i bought at a shop and paid with bank account",
            checksum="A",
            pk=1,
        )
        d2 = Document.objects.create(
            title="bank statement 1",
            content="things i paid for in august",
            pk=2,
            checksum="B",
        )
        d3 = Document.objects.create(
            title="bank statement 3",
            content="things i paid for in september",
            pk=3,
            checksum="C",
        )
        with index.open_index_writer() as writer:
            index.update_document(writer, d1)
            index.update_document(writer, d2)
            index.update_document(writer, d3)

        response = self.client.get("/api/documents/?query=added:[-1 week to now]")
        results = response.data["results"]
        # Expect 3 documents returned
        self.assertEqual(len(results), 3)

        for idx, subset in enumerate(
            [
                {"id": 1, "title": "invoice"},
                {"id": 2, "title": "bank statement 1"},
                {"id": 3, "title": "bank statement 3"},
            ],
        ):
            result = results[idx]
            # Assert subset in results
            self.assertDictEqual(result, {**result, **subset})

    @override_settings(
        TIME_ZONE="America/Chicago",
    )
    def test_search_added_in_last_week_with_timezone_behind(self):
        """
        GIVEN:
            - Two documents added right now
            - One document added over a week ago
            - The timezone is behind UTC time (-6)
        WHEN:
            - Query for documents added in the last 7 days
        THEN:
            - The two recent documents are returned
        """
        d1 = Document.objects.create(
            title="invoice",
            content="the thing i bought at a shop and paid with bank account",
            checksum="A",
            pk=1,
        )
        d2 = Document.objects.create(
            title="bank statement 1",
            content="things i paid for in august",
            pk=2,
            checksum="B",
        )
        d3 = Document.objects.create(
            title="bank statement 3",
            content="things i paid for in september",
            pk=3,
            checksum="C",
            # 7 days, 1 hour and 1 minute ago
            added=timezone.now() - timedelta(days=7, hours=1, minutes=1),
        )
        with index.open_index_writer() as writer:
            index.update_document(writer, d1)
            index.update_document(writer, d2)
            index.update_document(writer, d3)

        response = self.client.get("/api/documents/?query=added:[-1 week to now]")
        results = response.data["results"]

        # Expect 2 documents returned
        self.assertEqual(len(results), 2)

        for idx, subset in enumerate(
            [{"id": 1, "title": "invoice"}, {"id": 2, "title": "bank statement 1"}],
        ):
            result = results[idx]
            # Assert subset in results
            self.assertDictEqual(result, {**result, **subset})

    @override_settings(
        TIME_ZONE="Europe/Sofia",
    )
    def test_search_added_in_last_week_with_timezone_ahead(self):
        """
        GIVEN:
            - Two documents added right now
            - One document added over a week ago
            - The timezone is behind UTC time (+2)
        WHEN:
            - Query for documents added in the last 7 days
        THEN:
            - The two recent documents are returned
        """
        d1 = Document.objects.create(
            title="invoice",
            content="the thing i bought at a shop and paid with bank account",
            checksum="A",
            pk=1,
        )
        d2 = Document.objects.create(
            title="bank statement 1",
            content="things i paid for in august",
            pk=2,
            checksum="B",
        )
        d3 = Document.objects.create(
            title="bank statement 3",
            content="things i paid for in september",
            pk=3,
            checksum="C",
            # 7 days, 1 hour and 1 minute ago
            added=timezone.now() - timedelta(days=7, hours=1, minutes=1),
        )
        with index.open_index_writer() as writer:
            index.update_document(writer, d1)
            index.update_document(writer, d2)
            index.update_document(writer, d3)

        response = self.client.get("/api/documents/?query=added:[-1 week to now]")
        results = response.data["results"]

        # Expect 2 documents returned
        self.assertEqual(len(results), 2)

        for idx, subset in enumerate(
            [{"id": 1, "title": "invoice"}, {"id": 2, "title": "bank statement 1"}],
        ):
            result = results[idx]
            # Assert subset in results
            self.assertDictEqual(result, {**result, **subset})

    def test_search_added_in_last_month(self):
        """
        GIVEN:
            - One document added right now
            - One documents added about a week ago
            - One document added over 1 month
        WHEN:
            - Query for documents added in the last month
        THEN:
            - The two recent documents are returned
        """
        d1 = Document.objects.create(
            title="invoice",
            content="the thing i bought at a shop and paid with bank account",
            checksum="A",
            pk=1,
        )
        d2 = Document.objects.create(
            title="bank statement 1",
            content="things i paid for in august",
            pk=2,
            checksum="B",
            # 1 month, 1 day ago
            added=timezone.now() - relativedelta(months=1, days=1),
        )
        d3 = Document.objects.create(
            title="bank statement 3",
            content="things i paid for in september",
            pk=3,
            checksum="C",
            # 7 days, 1 hour and 1 minute ago
            added=timezone.now() - timedelta(days=7, hours=1, minutes=1),
        )

        with index.open_index_writer() as writer:
            index.update_document(writer, d1)
            index.update_document(writer, d2)
            index.update_document(writer, d3)

        response = self.client.get("/api/documents/?query=added:[-1 month to now]")
        results = response.data["results"]

        # Expect 2 documents returned
        self.assertEqual(len(results), 2)

        for idx, subset in enumerate(
            [{"id": 1, "title": "invoice"}, {"id": 3, "title": "bank statement 3"}],
        ):
            result = results[idx]
            # Assert subset in results
            self.assertDictEqual(result, {**result, **subset})

    @override_settings(
        TIME_ZONE="America/Denver",
    )
    def test_search_added_in_last_month_timezone_behind(self):
        """
        GIVEN:
            - One document added right now
            - One documents added about a week ago
            - One document added over 1 month
            - The timezone is behind UTC time (-6 or -7)
        WHEN:
            - Query for documents added in the last month
        THEN:
            - The two recent documents are returned
        """
        d1 = Document.objects.create(
            title="invoice",
            content="the thing i bought at a shop and paid with bank account",
            checksum="A",
            pk=1,
        )
        d2 = Document.objects.create(
            title="bank statement 1",
            content="things i paid for in august",
            pk=2,
            checksum="B",
            # 1 month, 1 day ago
            added=timezone.now() - relativedelta(months=1, days=1),
        )
        d3 = Document.objects.create(
            title="bank statement 3",
            content="things i paid for in september",
            pk=3,
            checksum="C",
            # 7 days, 1 hour and 1 minute ago
            added=timezone.now() - timedelta(days=7, hours=1, minutes=1),
        )

        with index.open_index_writer() as writer:
            index.update_document(writer, d1)
            index.update_document(writer, d2)
            index.update_document(writer, d3)

        response = self.client.get("/api/documents/?query=added:[-1 month to now]")
        results = response.data["results"]

        # Expect 2 documents returned
        self.assertEqual(len(results), 2)

        for idx, subset in enumerate(
            [{"id": 1, "title": "invoice"}, {"id": 3, "title": "bank statement 3"}],
        ):
            result = results[idx]
            # Assert subset in results
            self.assertDictEqual(result, {**result, **subset})

    @mock.patch("documents.index.autocomplete")
    def test_search_autocomplete(self, m):
        m.side_effect = lambda ix, term, limit: [term for _ in range(limit)]

        response = self.client.get("/api/search/autocomplete/?term=test")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 10)

        response = self.client.get("/api/search/autocomplete/?term=test&limit=20")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 20)

        response = self.client.get("/api/search/autocomplete/?term=test&limit=-1")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

        response = self.client.get("/api/search/autocomplete/")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

        response = self.client.get("/api/search/autocomplete/?term=")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 10)

    @pytest.mark.skip(reason="Not implemented yet")
    def test_search_spelling_correction(self):
        with AsyncWriter(index.open_index()) as writer:
            for i in range(55):
                doc = Document.objects.create(
                    checksum=str(i),
                    pk=i + 1,
                    title=f"Document {i+1}",
                    content=f"Things document {i+1}",
                )
                index.update_document(writer, doc)

        response = self.client.get("/api/search/?query=thing")
        correction = response.data["corrected_query"]

        self.assertEqual(correction, "things")

        response = self.client.get("/api/search/?query=things")
        correction = response.data["corrected_query"]

        self.assertEqual(correction, None)

    def test_search_more_like(self):
        d1 = Document.objects.create(
            title="invoice",
            content="the thing i bought at a shop and paid with bank account",
            checksum="A",
            pk=1,
        )
        d2 = Document.objects.create(
            title="bank statement 1",
            content="things i paid for in august",
            pk=2,
            checksum="B",
        )
        d3 = Document.objects.create(
            title="bank statement 3",
            content="things i paid for in september",
            pk=3,
            checksum="C",
        )
        with AsyncWriter(index.open_index()) as writer:
            index.update_document(writer, d1)
            index.update_document(writer, d2)
            index.update_document(writer, d3)

        response = self.client.get(f"/api/documents/?more_like_id={d2.id}")

        self.assertEqual(response.status_code, status.HTTP_200_OK)

        results = response.data["results"]

        self.assertEqual(len(results), 2)
        self.assertEqual(results[0]["id"], d3.id)
        self.assertEqual(results[1]["id"], d1.id)

    def test_search_filtering(self):
        t = Tag.objects.create(name="tag")
        t2 = Tag.objects.create(name="tag2")
        c = Correspondent.objects.create(name="correspondent")
        dt = DocumentType.objects.create(name="type")
        sp = StoragePath.objects.create(name="path")

        d1 = Document.objects.create(checksum="1", correspondent=c, content="test")
        d2 = Document.objects.create(checksum="2", document_type=dt, content="test")
        d3 = Document.objects.create(checksum="3", content="test")

        d3.tags.add(t)
        d3.tags.add(t2)
        d4 = Document.objects.create(
            checksum="4",
            created=timezone.make_aware(datetime.datetime(2020, 7, 13)),
            content="test",
        )
        d4.tags.add(t2)
        d5 = Document.objects.create(
            checksum="5",
            added=timezone.make_aware(datetime.datetime(2020, 7, 13)),
            content="test",
        )
        d6 = Document.objects.create(checksum="6", content="test2")
        d7 = Document.objects.create(checksum="7", storage_path=sp, content="test")

        with AsyncWriter(index.open_index()) as writer:
            for doc in Document.objects.all():
                index.update_document(writer, doc)

        def search_query(q):
            r = self.client.get("/api/documents/?query=test" + q)
            self.assertEqual(r.status_code, status.HTTP_200_OK)
            return [hit["id"] for hit in r.data["results"]]

        self.assertCountEqual(
            search_query(""),
            [d1.id, d2.id, d3.id, d4.id, d5.id, d7.id],
        )
        self.assertCountEqual(search_query("&is_tagged=true"), [d3.id, d4.id])
        self.assertCountEqual(
            search_query("&is_tagged=false"),
            [d1.id, d2.id, d5.id, d7.id],
        )
        self.assertCountEqual(search_query("&correspondent__id=" + str(c.id)), [d1.id])
        self.assertCountEqual(search_query("&document_type__id=" + str(dt.id)), [d2.id])
        self.assertCountEqual(search_query("&storage_path__id=" + str(sp.id)), [d7.id])

        self.assertCountEqual(
            search_query("&storage_path__isnull"),
            [d1.id, d2.id, d3.id, d4.id, d5.id],
        )
        self.assertCountEqual(
            search_query("&correspondent__isnull"),
            [d2.id, d3.id, d4.id, d5.id, d7.id],
        )
        self.assertCountEqual(
            search_query("&document_type__isnull"),
            [d1.id, d3.id, d4.id, d5.id, d7.id],
        )
        self.assertCountEqual(
            search_query("&tags__id__all=" + str(t.id) + "," + str(t2.id)),
            [d3.id],
        )
        self.assertCountEqual(search_query("&tags__id__all=" + str(t.id)), [d3.id])
        self.assertCountEqual(
            search_query("&tags__id__all=" + str(t2.id)),
            [d3.id, d4.id],
        )

        self.assertIn(
            d4.id,
            search_query(
                "&created__date__lt="
                + datetime.datetime(2020, 9, 2).strftime("%Y-%m-%d"),
            ),
        )
        self.assertNotIn(
            d4.id,
            search_query(
                "&created__date__gt="
                + datetime.datetime(2020, 9, 2).strftime("%Y-%m-%d"),
            ),
        )

        self.assertNotIn(
            d4.id,
            search_query(
                "&created__date__lt="
                + datetime.datetime(2020, 1, 2).strftime("%Y-%m-%d"),
            ),
        )
        self.assertIn(
            d4.id,
            search_query(
                "&created__date__gt="
                + datetime.datetime(2020, 1, 2).strftime("%Y-%m-%d"),
            ),
        )

        self.assertIn(
            d5.id,
            search_query(
                "&added__date__lt="
                + datetime.datetime(2020, 9, 2).strftime("%Y-%m-%d"),
            ),
        )
        self.assertNotIn(
            d5.id,
            search_query(
                "&added__date__gt="
                + datetime.datetime(2020, 9, 2).strftime("%Y-%m-%d"),
            ),
        )

        self.assertNotIn(
            d5.id,
            search_query(
                "&added__date__lt="
                + datetime.datetime(2020, 1, 2).strftime("%Y-%m-%d"),
            ),
        )
        self.assertIn(
            d5.id,
            search_query(
                "&added__date__gt="
                + datetime.datetime(2020, 1, 2).strftime("%Y-%m-%d"),
            ),
        )

    def test_search_sorting(self):
        c1 = Correspondent.objects.create(name="corres Ax")
        c2 = Correspondent.objects.create(name="corres Cx")
        c3 = Correspondent.objects.create(name="corres Bx")
        d1 = Document.objects.create(
            checksum="1",
            correspondent=c1,
            content="test",
            archive_serial_number=2,
            title="3",
        )
        d2 = Document.objects.create(
            checksum="2",
            correspondent=c2,
            content="test",
            archive_serial_number=3,
            title="2",
        )
        d3 = Document.objects.create(
            checksum="3",
            correspondent=c3,
            content="test",
            archive_serial_number=1,
            title="1",
        )

        with AsyncWriter(index.open_index()) as writer:
            for doc in Document.objects.all():
                index.update_document(writer, doc)

        def search_query(q):
            r = self.client.get("/api/documents/?query=test" + q)
            self.assertEqual(r.status_code, status.HTTP_200_OK)
            return [hit["id"] for hit in r.data["results"]]

        self.assertListEqual(
            search_query("&ordering=archive_serial_number"),
            [d3.id, d1.id, d2.id],
        )
        self.assertListEqual(
            search_query("&ordering=-archive_serial_number"),
            [d2.id, d1.id, d3.id],
        )
        self.assertListEqual(search_query("&ordering=title"), [d3.id, d2.id, d1.id])
        self.assertListEqual(search_query("&ordering=-title"), [d1.id, d2.id, d3.id])
        self.assertListEqual(
            search_query("&ordering=correspondent__name"),
            [d1.id, d3.id, d2.id],
        )
        self.assertListEqual(
            search_query("&ordering=-correspondent__name"),
            [d2.id, d3.id, d1.id],
        )

    def test_statistics(self):

        doc1 = Document.objects.create(title="none1", checksum="A")
        doc2 = Document.objects.create(title="none2", checksum="B")
        doc3 = Document.objects.create(title="none3", checksum="C")

        tag_inbox = Tag.objects.create(name="t1", is_inbox_tag=True)

        doc1.tags.add(tag_inbox)

        response = self.client.get("/api/statistics/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["documents_total"], 3)
        self.assertEqual(response.data["documents_inbox"], 1)

    def test_statistics_no_inbox_tag(self):
        Document.objects.create(title="none1", checksum="A")

        response = self.client.get("/api/statistics/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["documents_inbox"], None)

    def test_upload(self):

        self.consume_file_mock.return_value = celery.result.AsyncResult(
            id=str(uuid.uuid4()),
        )

        with open(
            os.path.join(os.path.dirname(__file__), "samples", "simple.pdf"),
            "rb",
        ) as f:
            response = self.client.post(
                "/api/documents/post_document/",
                {"document": f},
            )

        self.assertEqual(response.status_code, status.HTTP_200_OK)

        self.consume_file_mock.assert_called_once()

        input_doc, overrides = self.get_last_consume_delay_call_args()

        self.assertEqual(input_doc.original_file.name, "simple.pdf")
        self.assertIn(Path(settings.SCRATCH_DIR), input_doc.original_file.parents)
        self.assertIsNone(overrides.title)
        self.assertIsNone(overrides.correspondent_id)
        self.assertIsNone(overrides.document_type_id)
        self.assertIsNone(overrides.tag_ids)

    def test_upload_empty_metadata(self):

        self.consume_file_mock.return_value = celery.result.AsyncResult(
            id=str(uuid.uuid4()),
        )

        with open(
            os.path.join(os.path.dirname(__file__), "samples", "simple.pdf"),
            "rb",
        ) as f:
            response = self.client.post(
                "/api/documents/post_document/",
                {"document": f, "title": "", "correspondent": "", "document_type": ""},
            )

        self.assertEqual(response.status_code, status.HTTP_200_OK)

        self.consume_file_mock.assert_called_once()

        input_doc, overrides = self.get_last_consume_delay_call_args()

        self.assertEqual(input_doc.original_file.name, "simple.pdf")
        self.assertIn(Path(settings.SCRATCH_DIR), input_doc.original_file.parents)
        self.assertIsNone(overrides.title)
        self.assertIsNone(overrides.correspondent_id)
        self.assertIsNone(overrides.document_type_id)
        self.assertIsNone(overrides.tag_ids)

    def test_upload_invalid_form(self):

        self.consume_file_mock.return_value = celery.result.AsyncResult(
            id=str(uuid.uuid4()),
        )

        with open(
            os.path.join(os.path.dirname(__file__), "samples", "simple.pdf"),
            "rb",
        ) as f:
            response = self.client.post(
                "/api/documents/post_document/",
                {"documenst": f},
            )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.consume_file_mock.assert_not_called()

    def test_upload_invalid_file(self):

        self.consume_file_mock.return_value = celery.result.AsyncResult(
            id=str(uuid.uuid4()),
        )

        with open(
            os.path.join(os.path.dirname(__file__), "samples", "simple.zip"),
            "rb",
        ) as f:
            response = self.client.post(
                "/api/documents/post_document/",
                {"document": f},
            )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.consume_file_mock.assert_not_called()

    def test_upload_with_title(self):

        self.consume_file_mock.return_value = celery.result.AsyncResult(
            id=str(uuid.uuid4()),
        )

        with open(
            os.path.join(os.path.dirname(__file__), "samples", "simple.pdf"),
            "rb",
        ) as f:
            response = self.client.post(
                "/api/documents/post_document/",
                {"document": f, "title": "my custom title"},
            )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        self.consume_file_mock.assert_called_once()

        _, overrides = self.get_last_consume_delay_call_args()

        self.assertEqual(overrides.title, "my custom title")
        self.assertIsNone(overrides.correspondent_id)
        self.assertIsNone(overrides.document_type_id)
        self.assertIsNone(overrides.tag_ids)

    def test_upload_with_correspondent(self):

        self.consume_file_mock.return_value = celery.result.AsyncResult(
            id=str(uuid.uuid4()),
        )

        c = Correspondent.objects.create(name="test-corres")
        with open(
            os.path.join(os.path.dirname(__file__), "samples", "simple.pdf"),
            "rb",
        ) as f:
            response = self.client.post(
                "/api/documents/post_document/",
                {"document": f, "correspondent": c.id},
            )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        self.consume_file_mock.assert_called_once()

        _, overrides = self.get_last_consume_delay_call_args()

        self.assertEqual(overrides.correspondent_id, c.id)
        self.assertIsNone(overrides.title)
        self.assertIsNone(overrides.document_type_id)
        self.assertIsNone(overrides.tag_ids)

    def test_upload_with_invalid_correspondent(self):

        self.consume_file_mock.return_value = celery.result.AsyncResult(
            id=str(uuid.uuid4()),
        )

        with open(
            os.path.join(os.path.dirname(__file__), "samples", "simple.pdf"),
            "rb",
        ) as f:
            response = self.client.post(
                "/api/documents/post_document/",
                {"document": f, "correspondent": 3456},
            )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

        self.consume_file_mock.assert_not_called()

    def test_upload_with_document_type(self):

        self.consume_file_mock.return_value = celery.result.AsyncResult(
            id=str(uuid.uuid4()),
        )

        dt = DocumentType.objects.create(name="invoice")
        with open(
            os.path.join(os.path.dirname(__file__), "samples", "simple.pdf"),
            "rb",
        ) as f:
            response = self.client.post(
                "/api/documents/post_document/",
                {"document": f, "document_type": dt.id},
            )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        self.consume_file_mock.assert_called_once()

        _, overrides = self.get_last_consume_delay_call_args()

        self.assertEqual(overrides.document_type_id, dt.id)
        self.assertIsNone(overrides.correspondent_id)
        self.assertIsNone(overrides.title)
        self.assertIsNone(overrides.tag_ids)

    def test_upload_with_invalid_document_type(self):

        self.consume_file_mock.return_value = celery.result.AsyncResult(
            id=str(uuid.uuid4()),
        )

        with open(
            os.path.join(os.path.dirname(__file__), "samples", "simple.pdf"),
            "rb",
        ) as f:
            response = self.client.post(
                "/api/documents/post_document/",
                {"document": f, "document_type": 34578},
            )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

        self.consume_file_mock.assert_not_called()

    def test_upload_with_tags(self):

        self.consume_file_mock.return_value = celery.result.AsyncResult(
            id=str(uuid.uuid4()),
        )

        t1 = Tag.objects.create(name="tag1")
        t2 = Tag.objects.create(name="tag2")
        with open(
            os.path.join(os.path.dirname(__file__), "samples", "simple.pdf"),
            "rb",
        ) as f:
            response = self.client.post(
                "/api/documents/post_document/",
                {"document": f, "tags": [t2.id, t1.id]},
            )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        self.consume_file_mock.assert_called_once()

        _, overrides = self.get_last_consume_delay_call_args()

        self.assertCountEqual(overrides.tag_ids, [t1.id, t2.id])
        self.assertIsNone(overrides.document_type_id)
        self.assertIsNone(overrides.correspondent_id)
        self.assertIsNone(overrides.title)

    def test_upload_with_invalid_tags(self):

        self.consume_file_mock.return_value = celery.result.AsyncResult(
            id=str(uuid.uuid4()),
        )

        t1 = Tag.objects.create(name="tag1")
        t2 = Tag.objects.create(name="tag2")
        with open(
            os.path.join(os.path.dirname(__file__), "samples", "simple.pdf"),
            "rb",
        ) as f:
            response = self.client.post(
                "/api/documents/post_document/",
                {"document": f, "tags": [t2.id, t1.id, 734563]},
            )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

        self.consume_file_mock.assert_not_called()

    def test_upload_with_created(self):

        self.consume_file_mock.return_value = celery.result.AsyncResult(
            id=str(uuid.uuid4()),
        )

        created = datetime.datetime(
            2022,
            5,
            12,
            0,
            0,
            0,
            0,
            tzinfo=zoneinfo.ZoneInfo("America/Los_Angeles"),
        )
        with open(
            os.path.join(os.path.dirname(__file__), "samples", "simple.pdf"),
            "rb",
        ) as f:
            response = self.client.post(
                "/api/documents/post_document/",
                {"document": f, "created": created},
            )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        self.consume_file_mock.assert_called_once()

        _, overrides = self.get_last_consume_delay_call_args()

        self.assertEqual(overrides.created, created)

    def test_upload_with_asn(self):

        self.consume_file_mock.return_value = celery.result.AsyncResult(
            id=str(uuid.uuid4()),
        )

        with open(
            os.path.join(os.path.dirname(__file__), "samples", "simple.pdf"),
            "rb",
        ) as f:
            response = self.client.post(
                "/api/documents/post_document/",
                {"document": f, "archive_serial_number": 500},
            )

        self.assertEqual(response.status_code, status.HTTP_200_OK)

        self.consume_file_mock.assert_called_once()

        input_doc, overrides = self.get_last_consume_delay_call_args()

        self.assertEqual(input_doc.original_file.name, "simple.pdf")
        self.assertEqual(overrides.filename, "simple.pdf")
        self.assertIsNone(overrides.correspondent_id)
        self.assertIsNone(overrides.document_type_id)
        self.assertIsNone(overrides.tag_ids)
        self.assertEqual(500, overrides.asn)

    def test_get_metadata(self):
        doc = Document.objects.create(
            title="test",
            filename="file.pdf",
            mime_type="image/png",
            archive_checksum="A",
            archive_filename="archive.pdf",
        )

        source_file = os.path.join(
            os.path.dirname(__file__),
            "samples",
            "documents",
            "thumbnails",
            "0000001.webp",
        )
        archive_file = os.path.join(os.path.dirname(__file__), "samples", "simple.pdf")

        shutil.copy(source_file, doc.source_path)
        shutil.copy(archive_file, doc.archive_path)

        response = self.client.get(f"/api/documents/{doc.pk}/metadata/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        meta = response.data

        self.assertEqual(meta["original_mime_type"], "image/png")
        self.assertTrue(meta["has_archive_version"])
        self.assertEqual(len(meta["original_metadata"]), 0)
        self.assertGreater(len(meta["archive_metadata"]), 0)
        self.assertEqual(meta["media_filename"], "file.pdf")
        self.assertEqual(meta["archive_media_filename"], "archive.pdf")
        self.assertEqual(meta["original_size"], os.stat(source_file).st_size)
        self.assertEqual(meta["archive_size"], os.stat(archive_file).st_size)

    def test_get_metadata_invalid_doc(self):
        response = self.client.get("/api/documents/34576/metadata/")
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_get_metadata_no_archive(self):
        doc = Document.objects.create(
            title="test",
            filename="file.pdf",
            mime_type="application/pdf",
        )

        shutil.copy(
            os.path.join(os.path.dirname(__file__), "samples", "simple.pdf"),
            doc.source_path,
        )

        response = self.client.get(f"/api/documents/{doc.pk}/metadata/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        meta = response.data

        self.assertEqual(meta["original_mime_type"], "application/pdf")
        self.assertFalse(meta["has_archive_version"])
        self.assertGreater(len(meta["original_metadata"]), 0)
        self.assertIsNone(meta["archive_metadata"])
        self.assertIsNone(meta["archive_media_filename"])

    def test_get_metadata_missing_files(self):
        doc = Document.objects.create(
            title="test",
            filename="file.pdf",
            mime_type="application/pdf",
            archive_filename="file.pdf",
            archive_checksum="B",
            checksum="A",
        )

        response = self.client.get(f"/api/documents/{doc.pk}/metadata/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        meta = response.data

        self.assertTrue(meta["has_archive_version"])
        self.assertIsNone(meta["original_metadata"])
        self.assertIsNone(meta["original_size"])
        self.assertIsNone(meta["archive_metadata"])
        self.assertIsNone(meta["archive_size"])

    def test_get_empty_suggestions(self):
        doc = Document.objects.create(title="test", mime_type="application/pdf")

        response = self.client.get(f"/api/documents/{doc.pk}/suggestions/")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            response.data,
            {
                "correspondents": [],
                "tags": [],
                "document_types": [],
                "storage_paths": [],
                "dates": [],
            },
        )

    def test_get_suggestions_invalid_doc(self):
        response = self.client.get("/api/documents/34676/suggestions/")
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    @mock.patch("documents.views.match_storage_paths")
    @mock.patch("documents.views.match_document_types")
    @mock.patch("documents.views.match_tags")
    @mock.patch("documents.views.match_correspondents")
    @override_settings(NUMBER_OF_SUGGESTED_DATES=10)
    def test_get_suggestions(
        self,
        match_correspondents,
        match_tags,
        match_document_types,
        match_storage_paths,
    ):
        doc = Document.objects.create(
            title="test",
            mime_type="application/pdf",
            content="this is an invoice from 12.04.2022!",
        )

        match_correspondents.return_value = [Correspondent(id=88), Correspondent(id=2)]
        match_tags.return_value = [Tag(id=56), Tag(id=123)]
        match_document_types.return_value = [DocumentType(id=23)]
        match_storage_paths.return_value = [StoragePath(id=99), StoragePath(id=77)]

        response = self.client.get(f"/api/documents/{doc.pk}/suggestions/")
        self.assertEqual(
            response.data,
            {
                "correspondents": [88, 2],
                "tags": [56, 123],
                "document_types": [23],
                "storage_paths": [99, 77],
                "dates": ["2022-04-12"],
            },
        )

    def test_saved_views(self):
        u1 = User.objects.create_superuser("user1")
        u2 = User.objects.create_superuser("user2")

        v1 = SavedView.objects.create(
            owner=u1,
            name="test1",
            sort_field="",
            show_on_dashboard=False,
            show_in_sidebar=False,
        )
        v2 = SavedView.objects.create(
            owner=u2,
            name="test2",
            sort_field="",
            show_on_dashboard=False,
            show_in_sidebar=False,
        )
        v3 = SavedView.objects.create(
            owner=u2,
            name="test3",
            sort_field="",
            show_on_dashboard=False,
            show_in_sidebar=False,
        )

        response = self.client.get("/api/saved_views/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["count"], 0)

        self.assertEqual(
            self.client.get(f"/api/saved_views/{v1.id}/").status_code,
            status.HTTP_404_NOT_FOUND,
        )

        self.client.force_authenticate(user=u1)

        response = self.client.get("/api/saved_views/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["count"], 1)

        self.assertEqual(
            self.client.get(f"/api/saved_views/{v1.id}/").status_code,
            status.HTTP_200_OK,
        )

        self.client.force_authenticate(user=u2)

        response = self.client.get("/api/saved_views/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["count"], 2)

        self.assertEqual(
            self.client.get(f"/api/saved_views/{v1.id}/").status_code,
            status.HTTP_404_NOT_FOUND,
        )

    def test_create_update_patch(self):

        u1 = User.objects.create_user("user1")

        view = {
            "name": "test",
            "show_on_dashboard": True,
            "show_in_sidebar": True,
            "sort_field": "created2",
            "filter_rules": [{"rule_type": 4, "value": "test"}],
        }

        response = self.client.post("/api/saved_views/", view, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        v1 = SavedView.objects.get(name="test")
        self.assertEqual(v1.sort_field, "created2")
        self.assertEqual(v1.filter_rules.count(), 1)
        self.assertEqual(v1.owner, self.user)

        response = self.client.patch(
            f"/api/saved_views/{v1.id}/",
            {"show_in_sidebar": False},
            format="json",
        )

        v1 = SavedView.objects.get(id=v1.id)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertFalse(v1.show_in_sidebar)
        self.assertEqual(v1.filter_rules.count(), 1)

        view["filter_rules"] = [{"rule_type": 12, "value": "secret"}]

        response = self.client.put(f"/api/saved_views/{v1.id}/", view, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        v1 = SavedView.objects.get(id=v1.id)
        self.assertEqual(v1.filter_rules.count(), 1)
        self.assertEqual(v1.filter_rules.first().value, "secret")

        view["filter_rules"] = []

        response = self.client.put(f"/api/saved_views/{v1.id}/", view, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        v1 = SavedView.objects.get(id=v1.id)
        self.assertEqual(v1.filter_rules.count(), 0)

    def test_get_logs(self):
        log_data = "test\ntest2\n"
        with open(os.path.join(settings.LOGGING_DIR, "mail.log"), "w") as f:
            f.write(log_data)
        with open(os.path.join(settings.LOGGING_DIR, "paperless.log"), "w") as f:
            f.write(log_data)
        response = self.client.get("/api/logs/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertCountEqual(response.data, ["mail", "paperless"])

    def test_get_logs_only_when_exist(self):
        log_data = "test\ntest2\n"
        with open(os.path.join(settings.LOGGING_DIR, "paperless.log"), "w") as f:
            f.write(log_data)
        response = self.client.get("/api/logs/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertCountEqual(response.data, ["paperless"])

    def test_get_invalid_log(self):
        response = self.client.get("/api/logs/bogus_log/")
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    @override_settings(LOGGING_DIR="bogus_dir")
    def test_get_nonexistent_log(self):
        response = self.client.get("/api/logs/paperless/")
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_get_log(self):
        log_data = "test\ntest2\n"
        with open(os.path.join(settings.LOGGING_DIR, "paperless.log"), "w") as f:
            f.write(log_data)
        response = self.client.get("/api/logs/paperless/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertListEqual(response.data, ["test", "test2"])

    def test_invalid_regex_other_algorithm(self):
        for endpoint in ["correspondents", "tags", "document_types"]:
            response = self.client.post(
                f"/api/{endpoint}/",
                {
                    "name": "test",
                    "matching_algorithm": MatchingModel.MATCH_ANY,
                    "match": "[",
                },
                format="json",
            )
            self.assertEqual(response.status_code, status.HTTP_201_CREATED, endpoint)

    def test_invalid_regex(self):
        for endpoint in ["correspondents", "tags", "document_types"]:
            response = self.client.post(
                f"/api/{endpoint}/",
                {
                    "name": "test",
                    "matching_algorithm": MatchingModel.MATCH_REGEX,
                    "match": "[",
                },
                format="json",
            )
            self.assertEqual(
                response.status_code,
                status.HTTP_400_BAD_REQUEST,
                endpoint,
            )

    def test_valid_regex(self):
        for endpoint in ["correspondents", "tags", "document_types"]:
            response = self.client.post(
                f"/api/{endpoint}/",
                {
                    "name": "test",
                    "matching_algorithm": MatchingModel.MATCH_REGEX,
                    "match": "[0-9]",
                },
                format="json",
            )
            self.assertEqual(response.status_code, status.HTTP_201_CREATED, endpoint)

    def test_regex_no_algorithm(self):
        for endpoint in ["correspondents", "tags", "document_types"]:
            response = self.client.post(
                f"/api/{endpoint}/",
                {"name": "test", "match": "[0-9]"},
                format="json",
            )
            self.assertEqual(response.status_code, status.HTTP_201_CREATED, endpoint)

    def test_tag_color_default(self):
        response = self.client.post("/api/tags/", {"name": "tag"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(Tag.objects.get(id=response.data["id"]).color, "#a6cee3")
        self.assertEqual(
            self.client.get(f"/api/tags/{response.data['id']}/", format="json").data[
                "colour"
            ],
            1,
        )

    def test_tag_color(self):
        response = self.client.post(
            "/api/tags/",
            {"name": "tag", "colour": 3},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(Tag.objects.get(id=response.data["id"]).color, "#b2df8a")
        self.assertEqual(
            self.client.get(f"/api/tags/{response.data['id']}/", format="json").data[
                "colour"
            ],
            3,
        )

    def test_tag_color_invalid(self):
        response = self.client.post(
            "/api/tags/",
            {"name": "tag", "colour": 34},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_tag_color_custom(self):
        tag = Tag.objects.create(name="test", color="#abcdef")
        self.assertEqual(
            self.client.get(f"/api/tags/{tag.id}/", format="json").data["colour"],
            1,
        )

    def test_get_existing_comments(self):
        """
        GIVEN:
            - A document with a single comment
        WHEN:
            - API reuqest for document comments is made
        THEN:
            - The associated comment is returned
        """
        doc = Document.objects.create(
            title="test",
            mime_type="application/pdf",
            content="this is a document which will have comments!",
        )
        comment = Comment.objects.create(
            comment="This is a comment.",
            document=doc,
            user=self.user,
        )

        response = self.client.get(
            f"/api/documents/{doc.pk}/comments/",
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)

        resp_data = response.json()

        self.assertEqual(len(resp_data), 1)

        resp_data = resp_data[0]
        del resp_data["created"]

        self.assertDictEqual(
            resp_data,
            {
                "id": comment.id,
                "comment": comment.comment,
                "user": {
                    "id": comment.user.id,
                    "username": comment.user.username,
                    "first_name": comment.user.first_name,
                    "last_name": comment.user.last_name,
                },
            },
        )

    def test_create_comment(self):
        """
        GIVEN:
            - Existing document
        WHEN:
            - API request is made to add a comment
        THEN:
            - Comment is created and associated with document
        """
        doc = Document.objects.create(
            title="test",
            mime_type="application/pdf",
            content="this is a document which will have comments added",
        )
        resp = self.client.post(
            f"/api/documents/{doc.pk}/comments/",
            data={"comment": "this is a posted comment"},
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

        response = self.client.get(
            f"/api/documents/{doc.pk}/comments/",
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)

        resp_data = response.json()

        self.assertEqual(len(resp_data), 1)

        resp_data = resp_data[0]

        self.assertEqual(resp_data["comment"], "this is a posted comment")

    def test_delete_comment(self):
        """
        GIVEN:
            - Existing document
        WHEN:
            - API request is made to add a comment
        THEN:
            - Comment is created and associated with document
        """
        doc = Document.objects.create(
            title="test",
            mime_type="application/pdf",
            content="this is a document which will have comments!",
        )
        comment = Comment.objects.create(
            comment="This is a comment.",
            document=doc,
            user=self.user,
        )

        response = self.client.delete(
            f"/api/documents/{doc.pk}/comments/?id={comment.pk}",
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)

        self.assertEqual(len(Comment.objects.all()), 0)

    def test_get_comments_no_doc(self):
        """
        GIVEN:
            - A request to get comments from a non-existent document
        WHEN:
            - API request for document comments is made
        THEN:
            - HTTP status.HTTP_404_NOT_FOUND is returned
        """
        response = self.client.get(
            "/api/documents/500/comments/",
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)


class TestDocumentApiV2(DirectoriesMixin, APITestCase):
    def setUp(self):
        super().setUp()

        self.user = User.objects.create_superuser(username="temp_admin")

        self.client.force_authenticate(user=self.user)
        self.client.defaults["HTTP_ACCEPT"] = "application/json; version=2"

    def test_tag_validate_color(self):
        self.assertEqual(
            self.client.post(
                "/api/tags/",
                {"name": "test", "color": "#12fFaA"},
                format="json",
            ).status_code,
            status.HTTP_201_CREATED,
        )

        self.assertEqual(
            self.client.post(
                "/api/tags/",
                {"name": "test1", "color": "abcdef"},
                format="json",
            ).status_code,
            status.HTTP_400_BAD_REQUEST,
        )
        self.assertEqual(
            self.client.post(
                "/api/tags/",
                {"name": "test2", "color": "#abcdfg"},
                format="json",
            ).status_code,
            status.HTTP_400_BAD_REQUEST,
        )
        self.assertEqual(
            self.client.post(
                "/api/tags/",
                {"name": "test3", "color": "#asd"},
                format="json",
            ).status_code,
            status.HTTP_400_BAD_REQUEST,
        )
        self.assertEqual(
            self.client.post(
                "/api/tags/",
                {"name": "test4", "color": "#12121212"},
                format="json",
            ).status_code,
            status.HTTP_400_BAD_REQUEST,
        )

    def test_tag_text_color(self):
        t = Tag.objects.create(name="tag1", color="#000000")
        self.assertEqual(
            self.client.get(f"/api/tags/{t.id}/", format="json").data["text_color"],
            "#ffffff",
        )

        t.color = "#ffffff"
        t.save()
        self.assertEqual(
            self.client.get(f"/api/tags/{t.id}/", format="json").data["text_color"],
            "#000000",
        )

        t.color = "asdf"
        t.save()
        self.assertEqual(
            self.client.get(f"/api/tags/{t.id}/", format="json").data["text_color"],
            "#000000",
        )

        t.color = "123"
        t.save()
        self.assertEqual(
            self.client.get(f"/api/tags/{t.id}/", format="json").data["text_color"],
            "#000000",
        )


class TestApiUiSettings(DirectoriesMixin, APITestCase):

    ENDPOINT = "/api/ui_settings/"

    def setUp(self):
        super().setUp()
        self.test_user = User.objects.create_superuser(username="test")
        self.client.force_authenticate(user=self.test_user)

    def test_api_get_ui_settings(self):
        response = self.client.get(self.ENDPOINT, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertDictEqual(
            response.data["settings"],
            {
                "update_checking": {
                    "backend_setting": "default",
                },
            },
        )

    def test_api_set_ui_settings(self):
        settings = {
            "settings": {
                "dark_mode": {
                    "enabled": True,
                },
            },
        }

        response = self.client.post(
            self.ENDPOINT,
            json.dumps(settings),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)

        ui_settings = self.test_user.ui_settings
        self.assertDictEqual(
            ui_settings.settings,
            settings["settings"],
        )


class TestBulkEdit(DirectoriesMixin, APITestCase):
    def setUp(self):
        super().setUp()

        user = User.objects.create_superuser(username="temp_admin")
        self.client.force_authenticate(user=user)

        patcher = mock.patch("documents.bulk_edit.bulk_update_documents.delay")
        self.async_task = patcher.start()
        self.addCleanup(patcher.stop)
        self.c1 = Correspondent.objects.create(name="c1")
        self.c2 = Correspondent.objects.create(name="c2")
        self.dt1 = DocumentType.objects.create(name="dt1")
        self.dt2 = DocumentType.objects.create(name="dt2")
        self.t1 = Tag.objects.create(name="t1")
        self.t2 = Tag.objects.create(name="t2")
        self.doc1 = Document.objects.create(checksum="A", title="A")
        self.doc2 = Document.objects.create(
            checksum="B",
            title="B",
            correspondent=self.c1,
            document_type=self.dt1,
        )
        self.doc3 = Document.objects.create(
            checksum="C",
            title="C",
            correspondent=self.c2,
            document_type=self.dt2,
        )
        self.doc4 = Document.objects.create(checksum="D", title="D")
        self.doc5 = Document.objects.create(checksum="E", title="E")
        self.doc2.tags.add(self.t1)
        self.doc3.tags.add(self.t2)
        self.doc4.tags.add(self.t1, self.t2)
        self.sp1 = StoragePath.objects.create(name="sp1", path="Something/{checksum}")

    def test_set_correspondent(self):
        self.assertEqual(Document.objects.filter(correspondent=self.c2).count(), 1)
        bulk_edit.set_correspondent(
            [self.doc1.id, self.doc2.id, self.doc3.id],
            self.c2.id,
        )
        self.assertEqual(Document.objects.filter(correspondent=self.c2).count(), 3)
        self.async_task.assert_called_once()
        args, kwargs = self.async_task.call_args
        self.assertCountEqual(kwargs["document_ids"], [self.doc1.id, self.doc2.id])

    def test_unset_correspondent(self):
        self.assertEqual(Document.objects.filter(correspondent=self.c2).count(), 1)
        bulk_edit.set_correspondent([self.doc1.id, self.doc2.id, self.doc3.id], None)
        self.assertEqual(Document.objects.filter(correspondent=self.c2).count(), 0)
        self.async_task.assert_called_once()
        args, kwargs = self.async_task.call_args
        self.assertCountEqual(kwargs["document_ids"], [self.doc2.id, self.doc3.id])

    def test_set_document_type(self):
        self.assertEqual(Document.objects.filter(document_type=self.dt2).count(), 1)
        bulk_edit.set_document_type(
            [self.doc1.id, self.doc2.id, self.doc3.id],
            self.dt2.id,
        )
        self.assertEqual(Document.objects.filter(document_type=self.dt2).count(), 3)
        self.async_task.assert_called_once()
        args, kwargs = self.async_task.call_args
        self.assertCountEqual(kwargs["document_ids"], [self.doc1.id, self.doc2.id])

    def test_unset_document_type(self):
        self.assertEqual(Document.objects.filter(document_type=self.dt2).count(), 1)
        bulk_edit.set_document_type([self.doc1.id, self.doc2.id, self.doc3.id], None)
        self.assertEqual(Document.objects.filter(document_type=self.dt2).count(), 0)
        self.async_task.assert_called_once()
        args, kwargs = self.async_task.call_args
        self.assertCountEqual(kwargs["document_ids"], [self.doc2.id, self.doc3.id])

    def test_set_document_storage_path(self):
        """
        GIVEN:
            - 5 documents without defined storage path
        WHEN:
            - Bulk edit called to add storage path to 1 document
        THEN:
            - Single document storage path update
        """
        self.assertEqual(Document.objects.filter(storage_path=None).count(), 5)

        bulk_edit.set_storage_path(
            [self.doc1.id],
            self.sp1.id,
        )

        self.assertEqual(Document.objects.filter(storage_path=None).count(), 4)

        self.async_task.assert_called_once()
        args, kwargs = self.async_task.call_args

        self.assertCountEqual(kwargs["document_ids"], [self.doc1.id])

    def test_unset_document_storage_path(self):
        """
        GIVEN:
            - 4 documents without defined storage path
            - 1 document with a defined storage
        WHEN:
            - Bulk edit called to remove storage path from 1 document
        THEN:
            - Single document storage path removed
        """
        self.assertEqual(Document.objects.filter(storage_path=None).count(), 5)

        bulk_edit.set_storage_path(
            [self.doc1.id],
            self.sp1.id,
        )

        self.assertEqual(Document.objects.filter(storage_path=None).count(), 4)

        bulk_edit.set_storage_path(
            [self.doc1.id],
            None,
        )

        self.assertEqual(Document.objects.filter(storage_path=None).count(), 5)

        self.async_task.assert_called()
        args, kwargs = self.async_task.call_args

        self.assertCountEqual(kwargs["document_ids"], [self.doc1.id])

    def test_add_tag(self):
        self.assertEqual(Document.objects.filter(tags__id=self.t1.id).count(), 2)
        bulk_edit.add_tag(
            [self.doc1.id, self.doc2.id, self.doc3.id, self.doc4.id],
            self.t1.id,
        )
        self.assertEqual(Document.objects.filter(tags__id=self.t1.id).count(), 4)
        self.async_task.assert_called_once()
        args, kwargs = self.async_task.call_args
        self.assertCountEqual(kwargs["document_ids"], [self.doc1.id, self.doc3.id])

    def test_remove_tag(self):
        self.assertEqual(Document.objects.filter(tags__id=self.t1.id).count(), 2)
        bulk_edit.remove_tag([self.doc1.id, self.doc3.id, self.doc4.id], self.t1.id)
        self.assertEqual(Document.objects.filter(tags__id=self.t1.id).count(), 1)
        self.async_task.assert_called_once()
        args, kwargs = self.async_task.call_args
        self.assertCountEqual(kwargs["document_ids"], [self.doc4.id])

    def test_modify_tags(self):
        tag_unrelated = Tag.objects.create(name="unrelated")
        self.doc2.tags.add(tag_unrelated)
        self.doc3.tags.add(tag_unrelated)
        bulk_edit.modify_tags(
            [self.doc2.id, self.doc3.id],
            add_tags=[self.t2.id],
            remove_tags=[self.t1.id],
        )

        self.assertCountEqual(list(self.doc2.tags.all()), [self.t2, tag_unrelated])
        self.assertCountEqual(list(self.doc3.tags.all()), [self.t2, tag_unrelated])

        self.async_task.assert_called_once()
        args, kwargs = self.async_task.call_args
        # TODO: doc3 should not be affected, but the query for that is rather complicated
        self.assertCountEqual(kwargs["document_ids"], [self.doc2.id, self.doc3.id])

    def test_delete(self):
        self.assertEqual(Document.objects.count(), 5)
        bulk_edit.delete([self.doc1.id, self.doc2.id])
        self.assertEqual(Document.objects.count(), 3)
        self.assertCountEqual(
            [doc.id for doc in Document.objects.all()],
            [self.doc3.id, self.doc4.id, self.doc5.id],
        )

    @mock.patch("documents.serialisers.bulk_edit.set_correspondent")
    def test_api_set_correspondent(self, m):
        m.return_value = "OK"
        response = self.client.post(
            "/api/documents/bulk_edit/",
            json.dumps(
                {
                    "documents": [self.doc1.id],
                    "method": "set_correspondent",
                    "parameters": {"correspondent": self.c1.id},
                },
            ),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        m.assert_called_once()
        args, kwargs = m.call_args
        self.assertEqual(args[0], [self.doc1.id])
        self.assertEqual(kwargs["correspondent"], self.c1.id)

    @mock.patch("documents.serialisers.bulk_edit.set_correspondent")
    def test_api_unset_correspondent(self, m):
        m.return_value = "OK"
        response = self.client.post(
            "/api/documents/bulk_edit/",
            json.dumps(
                {
                    "documents": [self.doc1.id],
                    "method": "set_correspondent",
                    "parameters": {"correspondent": None},
                },
            ),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        m.assert_called_once()
        args, kwargs = m.call_args
        self.assertEqual(args[0], [self.doc1.id])
        self.assertIsNone(kwargs["correspondent"])

    @mock.patch("documents.serialisers.bulk_edit.set_document_type")
    def test_api_set_type(self, m):
        m.return_value = "OK"
        response = self.client.post(
            "/api/documents/bulk_edit/",
            json.dumps(
                {
                    "documents": [self.doc1.id],
                    "method": "set_document_type",
                    "parameters": {"document_type": self.dt1.id},
                },
            ),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        m.assert_called_once()
        args, kwargs = m.call_args
        self.assertEqual(args[0], [self.doc1.id])
        self.assertEqual(kwargs["document_type"], self.dt1.id)

    @mock.patch("documents.serialisers.bulk_edit.set_document_type")
    def test_api_unset_type(self, m):
        m.return_value = "OK"
        response = self.client.post(
            "/api/documents/bulk_edit/",
            json.dumps(
                {
                    "documents": [self.doc1.id],
                    "method": "set_document_type",
                    "parameters": {"document_type": None},
                },
            ),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        m.assert_called_once()
        args, kwargs = m.call_args
        self.assertEqual(args[0], [self.doc1.id])
        self.assertIsNone(kwargs["document_type"])

    @mock.patch("documents.serialisers.bulk_edit.add_tag")
    def test_api_add_tag(self, m):
        m.return_value = "OK"
        response = self.client.post(
            "/api/documents/bulk_edit/",
            json.dumps(
                {
                    "documents": [self.doc1.id],
                    "method": "add_tag",
                    "parameters": {"tag": self.t1.id},
                },
            ),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        m.assert_called_once()
        args, kwargs = m.call_args
        self.assertEqual(args[0], [self.doc1.id])
        self.assertEqual(kwargs["tag"], self.t1.id)

    @mock.patch("documents.serialisers.bulk_edit.remove_tag")
    def test_api_remove_tag(self, m):
        m.return_value = "OK"
        response = self.client.post(
            "/api/documents/bulk_edit/",
            json.dumps(
                {
                    "documents": [self.doc1.id],
                    "method": "remove_tag",
                    "parameters": {"tag": self.t1.id},
                },
            ),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        m.assert_called_once()
        args, kwargs = m.call_args
        self.assertEqual(args[0], [self.doc1.id])
        self.assertEqual(kwargs["tag"], self.t1.id)

    @mock.patch("documents.serialisers.bulk_edit.modify_tags")
    def test_api_modify_tags(self, m):
        m.return_value = "OK"
        response = self.client.post(
            "/api/documents/bulk_edit/",
            json.dumps(
                {
                    "documents": [self.doc1.id, self.doc3.id],
                    "method": "modify_tags",
                    "parameters": {
                        "add_tags": [self.t1.id],
                        "remove_tags": [self.t2.id],
                    },
                },
            ),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        m.assert_called_once()
        args, kwargs = m.call_args
        self.assertListEqual(args[0], [self.doc1.id, self.doc3.id])
        self.assertEqual(kwargs["add_tags"], [self.t1.id])
        self.assertEqual(kwargs["remove_tags"], [self.t2.id])

    @mock.patch("documents.serialisers.bulk_edit.modify_tags")
    def test_api_modify_tags_not_provided(self, m):
        """
        GIVEN:
            - API data to modify tags is missing modify_tags field
        WHEN:
            - API to edit tags is called
        THEN:
            - API returns HTTP 400
            - modify_tags is not called
        """
        m.return_value = "OK"
        response = self.client.post(
            "/api/documents/bulk_edit/",
            json.dumps(
                {
                    "documents": [self.doc1.id, self.doc3.id],
                    "method": "modify_tags",
                    "parameters": {
                        "add_tags": [self.t1.id],
                    },
                },
            ),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        m.assert_not_called()

    @mock.patch("documents.serialisers.bulk_edit.delete")
    def test_api_delete(self, m):
        m.return_value = "OK"
        response = self.client.post(
            "/api/documents/bulk_edit/",
            json.dumps(
                {"documents": [self.doc1.id], "method": "delete", "parameters": {}},
            ),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        m.assert_called_once()
        args, kwargs = m.call_args
        self.assertEqual(args[0], [self.doc1.id])
        self.assertEqual(len(kwargs), 0)

    @mock.patch("documents.serialisers.bulk_edit.set_storage_path")
    def test_api_set_storage_path(self, m):
        """
        GIVEN:
            - API data to set the storage path of a document
        WHEN:
            - API is called
        THEN:
            - set_storage_path is called with correct document IDs and storage_path ID
        """
        m.return_value = "OK"

        response = self.client.post(
            "/api/documents/bulk_edit/",
            json.dumps(
                {
                    "documents": [self.doc1.id],
                    "method": "set_storage_path",
                    "parameters": {"storage_path": self.sp1.id},
                },
            ),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        m.assert_called_once()
        args, kwargs = m.call_args

        self.assertListEqual(args[0], [self.doc1.id])
        self.assertEqual(kwargs["storage_path"], self.sp1.id)

    @mock.patch("documents.serialisers.bulk_edit.set_storage_path")
    def test_api_unset_storage_path(self, m):
        """
        GIVEN:
            - API data to clear/unset the storage path of a document
        WHEN:
            - API is called
        THEN:
            - set_storage_path is called with correct document IDs and None storage_path
        """
        m.return_value = "OK"

        response = self.client.post(
            "/api/documents/bulk_edit/",
            json.dumps(
                {
                    "documents": [self.doc1.id],
                    "method": "set_storage_path",
                    "parameters": {"storage_path": None},
                },
            ),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        m.assert_called_once()
        args, kwargs = m.call_args

        self.assertListEqual(args[0], [self.doc1.id])
        self.assertEqual(kwargs["storage_path"], None)

    def test_api_invalid_storage_path(self):
        """
        GIVEN:
            - API data to set the storage path of a document
            - Given storage_path ID isn't valid
        WHEN:
            - API is called
        THEN:
            - set_storage_path is called with correct document IDs and storage_path ID
        """
        response = self.client.post(
            "/api/documents/bulk_edit/",
            json.dumps(
                {
                    "documents": [self.doc1.id],
                    "method": "set_storage_path",
                    "parameters": {"storage_path": self.sp1.id + 10},
                },
            ),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.async_task.assert_not_called()

    def test_api_set_storage_path_not_provided(self):
        """
        GIVEN:
            - API data to set the storage path of a document
            - API data is missing storage path ID
        WHEN:
            - API is called
        THEN:
            - set_storage_path is called with correct document IDs and storage_path ID
        """
        response = self.client.post(
            "/api/documents/bulk_edit/",
            json.dumps(
                {
                    "documents": [self.doc1.id],
                    "method": "set_storage_path",
                    "parameters": {},
                },
            ),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.async_task.assert_not_called()

    def test_api_invalid_doc(self):
        self.assertEqual(Document.objects.count(), 5)
        response = self.client.post(
            "/api/documents/bulk_edit/",
            json.dumps({"documents": [-235], "method": "delete", "parameters": {}}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(Document.objects.count(), 5)

    def test_api_invalid_method(self):
        self.assertEqual(Document.objects.count(), 5)
        response = self.client.post(
            "/api/documents/bulk_edit/",
            json.dumps(
                {
                    "documents": [self.doc2.id],
                    "method": "exterminate",
                    "parameters": {},
                },
            ),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(Document.objects.count(), 5)

    def test_api_invalid_correspondent(self):
        self.assertEqual(self.doc2.correspondent, self.c1)
        response = self.client.post(
            "/api/documents/bulk_edit/",
            json.dumps(
                {
                    "documents": [self.doc2.id],
                    "method": "set_correspondent",
                    "parameters": {"correspondent": 345657},
                },
            ),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

        doc2 = Document.objects.get(id=self.doc2.id)
        self.assertEqual(doc2.correspondent, self.c1)

    def test_api_no_correspondent(self):
        response = self.client.post(
            "/api/documents/bulk_edit/",
            json.dumps(
                {
                    "documents": [self.doc2.id],
                    "method": "set_correspondent",
                    "parameters": {},
                },
            ),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_api_invalid_document_type(self):
        self.assertEqual(self.doc2.document_type, self.dt1)
        response = self.client.post(
            "/api/documents/bulk_edit/",
            json.dumps(
                {
                    "documents": [self.doc2.id],
                    "method": "set_document_type",
                    "parameters": {"document_type": 345657},
                },
            ),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

        doc2 = Document.objects.get(id=self.doc2.id)
        self.assertEqual(doc2.document_type, self.dt1)

    def test_api_no_document_type(self):
        response = self.client.post(
            "/api/documents/bulk_edit/",
            json.dumps(
                {
                    "documents": [self.doc2.id],
                    "method": "set_document_type",
                    "parameters": {},
                },
            ),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_api_add_invalid_tag(self):
        self.assertEqual(list(self.doc2.tags.all()), [self.t1])
        response = self.client.post(
            "/api/documents/bulk_edit/",
            json.dumps(
                {
                    "documents": [self.doc2.id],
                    "method": "add_tag",
                    "parameters": {"tag": 345657},
                },
            ),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

        self.assertEqual(list(self.doc2.tags.all()), [self.t1])

    def test_api_add_tag_no_tag(self):
        response = self.client.post(
            "/api/documents/bulk_edit/",
            json.dumps(
                {"documents": [self.doc2.id], "method": "add_tag", "parameters": {}},
            ),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_api_delete_invalid_tag(self):
        self.assertEqual(list(self.doc2.tags.all()), [self.t1])
        response = self.client.post(
            "/api/documents/bulk_edit/",
            json.dumps(
                {
                    "documents": [self.doc2.id],
                    "method": "remove_tag",
                    "parameters": {"tag": 345657},
                },
            ),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

        self.assertEqual(list(self.doc2.tags.all()), [self.t1])

    def test_api_delete_tag_no_tag(self):
        response = self.client.post(
            "/api/documents/bulk_edit/",
            json.dumps(
                {"documents": [self.doc2.id], "method": "remove_tag", "parameters": {}},
            ),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_api_modify_invalid_tags(self):
        self.assertEqual(list(self.doc2.tags.all()), [self.t1])
        response = self.client.post(
            "/api/documents/bulk_edit/",
            json.dumps(
                {
                    "documents": [self.doc2.id],
                    "method": "modify_tags",
                    "parameters": {
                        "add_tags": [self.t2.id, 1657],
                        "remove_tags": [1123123],
                    },
                },
            ),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_api_modify_tags_no_tags(self):
        response = self.client.post(
            "/api/documents/bulk_edit/",
            json.dumps(
                {
                    "documents": [self.doc2.id],
                    "method": "modify_tags",
                    "parameters": {"remove_tags": [1123123]},
                },
            ),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

        response = self.client.post(
            "/api/documents/bulk_edit/",
            json.dumps(
                {
                    "documents": [self.doc2.id],
                    "method": "modify_tags",
                    "parameters": {"add_tags": [self.t2.id, 1657]},
                },
            ),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_api_selection_data_empty(self):
        response = self.client.post(
            "/api/documents/selection_data/",
            json.dumps({"documents": []}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        for field, Entity in [
            ("selected_correspondents", Correspondent),
            ("selected_tags", Tag),
            ("selected_document_types", DocumentType),
        ]:
            self.assertEqual(len(response.data[field]), Entity.objects.count())
            for correspondent in response.data[field]:
                self.assertEqual(correspondent["document_count"], 0)
            self.assertCountEqual(
                map(lambda c: c["id"], response.data[field]),
                map(lambda c: c["id"], Entity.objects.values("id")),
            )

    def test_api_selection_data(self):
        response = self.client.post(
            "/api/documents/selection_data/",
            json.dumps(
                {"documents": [self.doc1.id, self.doc2.id, self.doc4.id, self.doc5.id]},
            ),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        self.assertCountEqual(
            response.data["selected_correspondents"],
            [
                {"id": self.c1.id, "document_count": 1},
                {"id": self.c2.id, "document_count": 0},
            ],
        )
        self.assertCountEqual(
            response.data["selected_tags"],
            [
                {"id": self.t1.id, "document_count": 2},
                {"id": self.t2.id, "document_count": 1},
            ],
        )
        self.assertCountEqual(
            response.data["selected_document_types"],
            [
                {"id": self.c1.id, "document_count": 1},
                {"id": self.c2.id, "document_count": 0},
            ],
        )

    @mock.patch("documents.serialisers.bulk_edit.set_permissions")
    def test_set_permissions(self, m):
        m.return_value = "OK"
        user1 = User.objects.create(username="user1")
        user2 = User.objects.create(username="user2")
        permissions = {
            "view": {
                "users": [user1.id, user2.id],
                "groups": None,
            },
            "change": {
                "users": [user1.id],
                "groups": None,
            },
        }

        response = self.client.post(
            "/api/documents/bulk_edit/",
            json.dumps(
                {
                    "documents": [self.doc2.id, self.doc3.id],
                    "method": "set_permissions",
                    "parameters": {"set_permissions": permissions},
                },
            ),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)

        m.assert_called_once()
        args, kwargs = m.call_args
        self.assertCountEqual(args[0], [self.doc2.id, self.doc3.id])
        self.assertEqual(len(kwargs["set_permissions"]["view"]["users"]), 2)


class TestBulkDownload(DirectoriesMixin, APITestCase):

    ENDPOINT = "/api/documents/bulk_download/"

    def setUp(self):
        super().setUp()

        user = User.objects.create_superuser(username="temp_admin")
        self.client.force_authenticate(user=user)

        self.doc1 = Document.objects.create(title="unrelated", checksum="A")
        self.doc2 = Document.objects.create(
            title="document A",
            filename="docA.pdf",
            mime_type="application/pdf",
            checksum="B",
            created=timezone.make_aware(datetime.datetime(2021, 1, 1)),
        )
        self.doc2b = Document.objects.create(
            title="document A",
            filename="docA2.pdf",
            mime_type="application/pdf",
            checksum="D",
            created=timezone.make_aware(datetime.datetime(2021, 1, 1)),
        )
        self.doc3 = Document.objects.create(
            title="document B",
            filename="docB.jpg",
            mime_type="image/jpeg",
            checksum="C",
            created=timezone.make_aware(datetime.datetime(2020, 3, 21)),
            archive_filename="docB.pdf",
            archive_checksum="D",
        )

        shutil.copy(
            os.path.join(os.path.dirname(__file__), "samples", "simple.pdf"),
            self.doc2.source_path,
        )
        shutil.copy(
            os.path.join(os.path.dirname(__file__), "samples", "simple.png"),
            self.doc2b.source_path,
        )
        shutil.copy(
            os.path.join(os.path.dirname(__file__), "samples", "simple.jpg"),
            self.doc3.source_path,
        )
        shutil.copy(
            os.path.join(os.path.dirname(__file__), "samples", "test_with_bom.pdf"),
            self.doc3.archive_path,
        )

    def test_download_originals(self):
        response = self.client.post(
            self.ENDPOINT,
            json.dumps(
                {"documents": [self.doc2.id, self.doc3.id], "content": "originals"},
            ),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response["Content-Type"], "application/zip")

        with zipfile.ZipFile(io.BytesIO(response.content)) as zipf:
            self.assertEqual(len(zipf.filelist), 2)
            self.assertIn("2021-01-01 document A.pdf", zipf.namelist())
            self.assertIn("2020-03-21 document B.jpg", zipf.namelist())

            with self.doc2.source_file as f:
                self.assertEqual(f.read(), zipf.read("2021-01-01 document A.pdf"))

            with self.doc3.source_file as f:
                self.assertEqual(f.read(), zipf.read("2020-03-21 document B.jpg"))

    def test_download_default(self):
        response = self.client.post(
            self.ENDPOINT,
            json.dumps({"documents": [self.doc2.id, self.doc3.id]}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response["Content-Type"], "application/zip")

        with zipfile.ZipFile(io.BytesIO(response.content)) as zipf:
            self.assertEqual(len(zipf.filelist), 2)
            self.assertIn("2021-01-01 document A.pdf", zipf.namelist())
            self.assertIn("2020-03-21 document B.pdf", zipf.namelist())

            with self.doc2.source_file as f:
                self.assertEqual(f.read(), zipf.read("2021-01-01 document A.pdf"))

            with self.doc3.archive_file as f:
                self.assertEqual(f.read(), zipf.read("2020-03-21 document B.pdf"))

    def test_download_both(self):
        response = self.client.post(
            self.ENDPOINT,
            json.dumps({"documents": [self.doc2.id, self.doc3.id], "content": "both"}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response["Content-Type"], "application/zip")

        with zipfile.ZipFile(io.BytesIO(response.content)) as zipf:
            self.assertEqual(len(zipf.filelist), 3)
            self.assertIn("originals/2021-01-01 document A.pdf", zipf.namelist())
            self.assertIn("archive/2020-03-21 document B.pdf", zipf.namelist())
            self.assertIn("originals/2020-03-21 document B.jpg", zipf.namelist())

            with self.doc2.source_file as f:
                self.assertEqual(
                    f.read(),
                    zipf.read("originals/2021-01-01 document A.pdf"),
                )

            with self.doc3.archive_file as f:
                self.assertEqual(
                    f.read(),
                    zipf.read("archive/2020-03-21 document B.pdf"),
                )

            with self.doc3.source_file as f:
                self.assertEqual(
                    f.read(),
                    zipf.read("originals/2020-03-21 document B.jpg"),
                )

    def test_filename_clashes(self):
        response = self.client.post(
            self.ENDPOINT,
            json.dumps({"documents": [self.doc2.id, self.doc2b.id]}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response["Content-Type"], "application/zip")

        with zipfile.ZipFile(io.BytesIO(response.content)) as zipf:
            self.assertEqual(len(zipf.filelist), 2)

            self.assertIn("2021-01-01 document A.pdf", zipf.namelist())
            self.assertIn("2021-01-01 document A_01.pdf", zipf.namelist())

            with self.doc2.source_file as f:
                self.assertEqual(f.read(), zipf.read("2021-01-01 document A.pdf"))

            with self.doc2b.source_file as f:
                self.assertEqual(f.read(), zipf.read("2021-01-01 document A_01.pdf"))

    def test_compression(self):
        response = self.client.post(
            self.ENDPOINT,
            json.dumps(
                {"documents": [self.doc2.id, self.doc2b.id], "compression": "lzma"},
            ),
            content_type="application/json",
        )

    @override_settings(FILENAME_FORMAT="{correspondent}/{title}")
    def test_formatted_download_originals(self):
        """
        GIVEN:
            - Defined file naming format
        WHEN:
            - Bulk download request for original documents
            - Bulk download request requests to follow format
        THEN:
            - Files defined in resulting zipfile are formatted
        """

        c = Correspondent.objects.create(name="test")
        c2 = Correspondent.objects.create(name="a space name")

        self.doc2.correspondent = c
        self.doc2.title = "This is Doc 2"
        self.doc2.save()

        self.doc3.correspondent = c2
        self.doc3.title = "Title 2 - Doc 3"
        self.doc3.save()

        response = self.client.post(
            self.ENDPOINT,
            json.dumps(
                {
                    "documents": [self.doc2.id, self.doc3.id],
                    "content": "originals",
                    "follow_formatting": True,
                },
            ),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response["Content-Type"], "application/zip")

        with zipfile.ZipFile(io.BytesIO(response.content)) as zipf:
            self.assertEqual(len(zipf.filelist), 2)
            self.assertIn("a space name/Title 2 - Doc 3.jpg", zipf.namelist())
            self.assertIn("test/This is Doc 2.pdf", zipf.namelist())

            with self.doc2.source_file as f:
                self.assertEqual(f.read(), zipf.read("test/This is Doc 2.pdf"))

            with self.doc3.source_file as f:
                self.assertEqual(
                    f.read(),
                    zipf.read("a space name/Title 2 - Doc 3.jpg"),
                )

    @override_settings(FILENAME_FORMAT="somewhere/{title}")
    def test_formatted_download_archive(self):
        """
        GIVEN:
            - Defined file naming format
        WHEN:
            - Bulk download request for archive documents
            - Bulk download request requests to follow format
        THEN:
            - Files defined in resulting zipfile are formatted
        """

        self.doc2.title = "This is Doc 2"
        self.doc2.save()

        self.doc3.title = "Title 2 - Doc 3"
        self.doc3.save()
        print(self.doc3.archive_path)
        print(self.doc3.archive_filename)

        response = self.client.post(
            self.ENDPOINT,
            json.dumps(
                {
                    "documents": [self.doc2.id, self.doc3.id],
                    "follow_formatting": True,
                },
            ),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response["Content-Type"], "application/zip")

        with zipfile.ZipFile(io.BytesIO(response.content)) as zipf:
            self.assertEqual(len(zipf.filelist), 2)
            self.assertIn("somewhere/This is Doc 2.pdf", zipf.namelist())
            self.assertIn("somewhere/Title 2 - Doc 3.pdf", zipf.namelist())

            with self.doc2.source_file as f:
                self.assertEqual(f.read(), zipf.read("somewhere/This is Doc 2.pdf"))

            with self.doc3.archive_file as f:
                self.assertEqual(f.read(), zipf.read("somewhere/Title 2 - Doc 3.pdf"))

    @override_settings(FILENAME_FORMAT="{document_type}/{title}")
    def test_formatted_download_both(self):
        """
        GIVEN:
            - Defined file naming format
        WHEN:
            - Bulk download request for original documents and archive documents
            - Bulk download request requests to follow format
        THEN:
            - Files defined in resulting zipfile are formatted
        """

        dc1 = DocumentType.objects.create(name="bill")
        dc2 = DocumentType.objects.create(name="statement")

        self.doc2.document_type = dc1
        self.doc2.title = "This is Doc 2"
        self.doc2.save()

        self.doc3.document_type = dc2
        self.doc3.title = "Title 2 - Doc 3"
        self.doc3.save()

        response = self.client.post(
            self.ENDPOINT,
            json.dumps(
                {
                    "documents": [self.doc2.id, self.doc3.id],
                    "content": "both",
                    "follow_formatting": True,
                },
            ),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response["Content-Type"], "application/zip")

        with zipfile.ZipFile(io.BytesIO(response.content)) as zipf:
            self.assertEqual(len(zipf.filelist), 3)
            self.assertIn("originals/bill/This is Doc 2.pdf", zipf.namelist())
            self.assertIn("archive/statement/Title 2 - Doc 3.pdf", zipf.namelist())
            self.assertIn("originals/statement/Title 2 - Doc 3.jpg", zipf.namelist())

            with self.doc2.source_file as f:
                self.assertEqual(
                    f.read(),
                    zipf.read("originals/bill/This is Doc 2.pdf"),
                )

            with self.doc3.archive_file as f:
                self.assertEqual(
                    f.read(),
                    zipf.read("archive/statement/Title 2 - Doc 3.pdf"),
                )

            with self.doc3.source_file as f:
                self.assertEqual(
                    f.read(),
                    zipf.read("originals/statement/Title 2 - Doc 3.jpg"),
                )


class TestApiAuth(DirectoriesMixin, APITestCase):
    def test_auth_required(self):

        d = Document.objects.create(title="Test")

        self.assertEqual(
            self.client.get("/api/documents/").status_code,
            status.HTTP_401_UNAUTHORIZED,
        )

        self.assertEqual(
            self.client.get(f"/api/documents/{d.id}/").status_code,
            status.HTTP_401_UNAUTHORIZED,
        )
        self.assertEqual(
            self.client.get(f"/api/documents/{d.id}/download/").status_code,
            status.HTTP_401_UNAUTHORIZED,
        )
        self.assertEqual(
            self.client.get(f"/api/documents/{d.id}/preview/").status_code,
            status.HTTP_401_UNAUTHORIZED,
        )
        self.assertEqual(
            self.client.get(f"/api/documents/{d.id}/thumb/").status_code,
            status.HTTP_401_UNAUTHORIZED,
        )

        self.assertEqual(
            self.client.get("/api/tags/").status_code,
            status.HTTP_401_UNAUTHORIZED,
        )
        self.assertEqual(
            self.client.get("/api/correspondents/").status_code,
            status.HTTP_401_UNAUTHORIZED,
        )
        self.assertEqual(
            self.client.get("/api/document_types/").status_code,
            status.HTTP_401_UNAUTHORIZED,
        )

        self.assertEqual(
            self.client.get("/api/logs/").status_code,
            status.HTTP_401_UNAUTHORIZED,
        )
        self.assertEqual(
            self.client.get("/api/saved_views/").status_code,
            status.HTTP_401_UNAUTHORIZED,
        )

        self.assertEqual(
            self.client.get("/api/search/autocomplete/").status_code,
            status.HTTP_401_UNAUTHORIZED,
        )
        self.assertEqual(
            self.client.get("/api/documents/bulk_edit/").status_code,
            status.HTTP_401_UNAUTHORIZED,
        )
        self.assertEqual(
            self.client.get("/api/documents/bulk_download/").status_code,
            status.HTTP_401_UNAUTHORIZED,
        )
        self.assertEqual(
            self.client.get("/api/documents/selection_data/").status_code,
            status.HTTP_401_UNAUTHORIZED,
        )

    def test_api_version_no_auth(self):

        response = self.client.get("/api/")
        self.assertNotIn("X-Api-Version", response)
        self.assertNotIn("X-Version", response)

    def test_api_version_with_auth(self):
        user = User.objects.create_superuser(username="test")
        self.client.force_authenticate(user)
        response = self.client.get("/api/")
        self.assertIn("X-Api-Version", response)
        self.assertIn("X-Version", response)

    def test_api_insufficient_permissions(self):
        user = User.objects.create_user(username="test")
        self.client.force_authenticate(user)

        d = Document.objects.create(title="Test")

        self.assertEqual(
            self.client.get("/api/documents/").status_code,
            status.HTTP_403_FORBIDDEN,
        )

        self.assertEqual(
            self.client.get("/api/tags/").status_code,
            status.HTTP_403_FORBIDDEN,
        )
        self.assertEqual(
            self.client.get("/api/correspondents/").status_code,
            status.HTTP_403_FORBIDDEN,
        )
        self.assertEqual(
            self.client.get("/api/document_types/").status_code,
            status.HTTP_403_FORBIDDEN,
        )

        self.assertEqual(
            self.client.get("/api/logs/").status_code,
            status.HTTP_403_FORBIDDEN,
        )
        self.assertEqual(
            self.client.get("/api/saved_views/").status_code,
            status.HTTP_403_FORBIDDEN,
        )

    def test_api_sufficient_permissions(self):
        user = User.objects.create_user(username="test")
        user.user_permissions.add(*Permission.objects.all())
        self.client.force_authenticate(user)

        d = Document.objects.create(title="Test")

        self.assertEqual(
            self.client.get("/api/documents/").status_code,
            status.HTTP_200_OK,
        )

        self.assertEqual(self.client.get("/api/tags/").status_code, status.HTTP_200_OK)
        self.assertEqual(
            self.client.get("/api/correspondents/").status_code,
            status.HTTP_200_OK,
        )
        self.assertEqual(
            self.client.get("/api/document_types/").status_code,
            status.HTTP_200_OK,
        )

        self.assertEqual(self.client.get("/api/logs/").status_code, status.HTTP_200_OK)
        self.assertEqual(
            self.client.get("/api/saved_views/").status_code,
            status.HTTP_200_OK,
        )

    def test_object_permissions(self):
        user1 = User.objects.create_user(username="test1")
        user2 = User.objects.create_user(username="test2")
        user1.user_permissions.add(*Permission.objects.filter(codename="view_document"))
        self.client.force_authenticate(user1)

        self.assertEqual(
            self.client.get("/api/documents/").status_code,
            status.HTTP_200_OK,
        )

        d = Document.objects.create(title="Test", content="the content 1", checksum="1")

        # no owner
        self.assertEqual(
            self.client.get(f"/api/documents/{d.id}/").status_code,
            status.HTTP_200_OK,
        )

        d2 = Document.objects.create(
            title="Test 2",
            content="the content 2",
            checksum="2",
            owner=user2,
        )

        self.assertEqual(
            self.client.get(f"/api/documents/{d2.id}/").status_code,
            status.HTTP_404_NOT_FOUND,
        )


class TestApiRemoteVersion(DirectoriesMixin, APITestCase):
    ENDPOINT = "/api/remote_version/"

    def setUp(self):
        super().setUp()

    @mock.patch("urllib.request.urlopen")
    def test_remote_version_enabled_no_update_prefix(self, urlopen_mock):

        cm = MagicMock()
        cm.getcode.return_value = status.HTTP_200_OK
        cm.read.return_value = json.dumps({"tag_name": "ngx-1.6.0"}).encode()
        cm.__enter__.return_value = cm
        urlopen_mock.return_value = cm

        response = self.client.get(self.ENDPOINT)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertDictEqual(
            response.data,
            {
                "version": "1.6.0",
                "update_available": False,
            },
        )

    @mock.patch("urllib.request.urlopen")
    def test_remote_version_enabled_no_update_no_prefix(self, urlopen_mock):

        cm = MagicMock()
        cm.getcode.return_value = status.HTTP_200_OK
        cm.read.return_value = json.dumps(
            {"tag_name": version.__full_version_str__},
        ).encode()
        cm.__enter__.return_value = cm
        urlopen_mock.return_value = cm

        response = self.client.get(self.ENDPOINT)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertDictEqual(
            response.data,
            {
                "version": version.__full_version_str__,
                "update_available": False,
            },
        )

    @mock.patch("urllib.request.urlopen")
    def test_remote_version_enabled_update(self, urlopen_mock):

        new_version = (
            version.__version__[0],
            version.__version__[1],
            version.__version__[2] + 1,
        )
        new_version_str = ".".join(map(str, new_version))

        cm = MagicMock()
        cm.getcode.return_value = status.HTTP_200_OK
        cm.read.return_value = json.dumps(
            {"tag_name": new_version_str},
        ).encode()
        cm.__enter__.return_value = cm
        urlopen_mock.return_value = cm

        response = self.client.get(self.ENDPOINT)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertDictEqual(
            response.data,
            {
                "version": new_version_str,
                "update_available": True,
            },
        )

    @mock.patch("urllib.request.urlopen")
    def test_remote_version_bad_json(self, urlopen_mock):

        cm = MagicMock()
        cm.getcode.return_value = status.HTTP_200_OK
        cm.read.return_value = b'{ "blah":'
        cm.__enter__.return_value = cm
        urlopen_mock.return_value = cm

        response = self.client.get(self.ENDPOINT)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertDictEqual(
            response.data,
            {
                "version": "0.0.0",
                "update_available": False,
            },
        )

    @mock.patch("urllib.request.urlopen")
    def test_remote_version_exception(self, urlopen_mock):

        cm = MagicMock()
        cm.getcode.return_value = status.HTTP_200_OK
        cm.read.side_effect = urllib.error.URLError("an error")
        cm.__enter__.return_value = cm
        urlopen_mock.return_value = cm

        response = self.client.get(self.ENDPOINT)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertDictEqual(
            response.data,
            {
                "version": "0.0.0",
                "update_available": False,
            },
        )


class TestApiStoragePaths(DirectoriesMixin, APITestCase):
    ENDPOINT = "/api/storage_paths/"

    def setUp(self) -> None:
        super().setUp()

        user = User.objects.create_superuser(username="temp_admin")
        self.client.force_authenticate(user=user)

        self.sp1 = StoragePath.objects.create(name="sp1", path="Something/{checksum}")

    def test_api_get_storage_path(self):
        """
        GIVEN:
            - API request to get all storage paths
        WHEN:
            - API is called
        THEN:
            - Existing storage paths are returned
        """
        response = self.client.get(self.ENDPOINT, format="json")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["count"], 1)

        resp_storage_path = response.data["results"][0]
        self.assertEqual(resp_storage_path["id"], self.sp1.id)
        self.assertEqual(resp_storage_path["path"], self.sp1.path)

    def test_api_create_storage_path(self):
        """
        GIVEN:
            - API request to create a storage paths
        WHEN:
            - API is called
        THEN:
            - Correct HTTP response
            - New storage path is created
        """
        response = self.client.post(
            self.ENDPOINT,
            json.dumps(
                {
                    "name": "A storage path",
                    "path": "Somewhere/{asn}",
                },
            ),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(StoragePath.objects.count(), 2)

    def test_api_create_invalid_storage_path(self):
        """
        GIVEN:
            - API request to create a storage paths
            - Storage path format is incorrect
        WHEN:
            - API is called
        THEN:
            - Correct HTTP 400 response
            - No storage path is created
        """
        response = self.client.post(
            self.ENDPOINT,
            json.dumps(
                {
                    "name": "Another storage path",
                    "path": "Somewhere/{correspdent}",
                },
            ),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(StoragePath.objects.count(), 1)

    def test_api_storage_path_placeholders(self):
        """
        GIVEN:
            - API request to create a storage path with placeholders
            - Storage path is valid
        WHEN:
            - API is called
        THEN:
            - Correct HTTP response
            - New storage path is created
        """
        response = self.client.post(
            self.ENDPOINT,
            json.dumps(
                {
                    "name": "Storage path with placeholders",
                    "path": "{title}/{correspondent}/{document_type}/{created}/{created_year}"
                    "/{created_year_short}/{created_month}/{created_month_name}"
                    "/{created_month_name_short}/{created_day}/{added}/{added_year}"
                    "/{added_year_short}/{added_month}/{added_month_name}"
                    "/{added_month_name_short}/{added_day}/{asn}/{tags}"
                    "/{tag_list}/",
                },
            ),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(StoragePath.objects.count(), 2)

    @mock.patch("documents.bulk_edit.bulk_update_documents.delay")
    def test_api_update_storage_path(self, bulk_update_mock):
        """
        GIVEN:
            - API request to get all storage paths
        WHEN:
            - API is called
        THEN:
            - Existing storage paths are returned
        """
        document = Document.objects.create(
            mime_type="application/pdf",
            storage_path=self.sp1,
        )
        response = self.client.patch(
            f"{self.ENDPOINT}{self.sp1.pk}/",
            data={
                "path": "somewhere/{created} - {title}",
            },
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)

        bulk_update_mock.assert_called_once()

        args, _ = bulk_update_mock.call_args

        self.assertCountEqual([document.pk], args[0])


class TestTasks(DirectoriesMixin, APITestCase):
    ENDPOINT = "/api/tasks/"
    ENDPOINT_ACKNOWLEDGE = "/api/acknowledge_tasks/"

    def setUp(self):
        super().setUp()

        self.user = User.objects.create_superuser(username="temp_admin")
        self.client.force_authenticate(user=self.user)

    def test_get_tasks(self):
        """
        GIVEN:
            - Attempted celery tasks
        WHEN:
            - API call is made to get tasks
        THEN:
            - Attempting and pending tasks are serialized and provided
        """

        task1 = PaperlessTask.objects.create(
            task_id=str(uuid.uuid4()),
            task_file_name="task_one.pdf",
        )

        task2 = PaperlessTask.objects.create(
            task_id=str(uuid.uuid4()),
            task_file_name="task_two.pdf",
        )

        response = self.client.get(self.ENDPOINT)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 2)
        returned_task1 = response.data[1]
        returned_task2 = response.data[0]

        self.assertEqual(returned_task1["task_id"], task1.task_id)
        self.assertEqual(returned_task1["status"], celery.states.PENDING)
        self.assertEqual(returned_task1["task_file_name"], task1.task_file_name)

        self.assertEqual(returned_task2["task_id"], task2.task_id)
        self.assertEqual(returned_task2["status"], celery.states.PENDING)
        self.assertEqual(returned_task2["task_file_name"], task2.task_file_name)

    def test_get_single_task_status(self):
        """
        GIVEN
            - Query parameter for a valid task ID
        WHEN:
            - API call is made to get task status
        THEN:
            - Single task data is returned
        """

        id1 = str(uuid.uuid4())
        task1 = PaperlessTask.objects.create(
            task_id=id1,
            task_file_name="task_one.pdf",
        )

        _ = PaperlessTask.objects.create(
            task_id=str(uuid.uuid4()),
            task_file_name="task_two.pdf",
        )

        response = self.client.get(self.ENDPOINT + f"?task_id={id1}")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        returned_task1 = response.data[0]

        self.assertEqual(returned_task1["task_id"], task1.task_id)

    def test_get_single_task_status_not_valid(self):
        """
        GIVEN
            - Query parameter for a non-existent task ID
        WHEN:
            - API call is made to get task status
        THEN:
            - No task data is returned
        """
        task1 = PaperlessTask.objects.create(
            task_id=str(uuid.uuid4()),
            task_file_name="task_one.pdf",
        )

        _ = PaperlessTask.objects.create(
            task_id=str(uuid.uuid4()),
            task_file_name="task_two.pdf",
        )

        response = self.client.get(self.ENDPOINT + "?task_id=bad-task-id")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 0)

    def test_acknowledge_tasks(self):
        """
        GIVEN:
            - Attempted celery tasks
        WHEN:
            - API call is made to get mark task as acknowledged
        THEN:
            - Task is marked as acknowledged
        """
        task = PaperlessTask.objects.create(
            task_id=str(uuid.uuid4()),
            task_file_name="task_one.pdf",
        )

        response = self.client.get(self.ENDPOINT)
        self.assertEqual(len(response.data), 1)

        response = self.client.post(
            self.ENDPOINT_ACKNOWLEDGE,
            {"tasks": [task.id]},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        response = self.client.get(self.ENDPOINT)
        self.assertEqual(len(response.data), 0)

    def test_task_result_no_error(self):
        """
        GIVEN:
            - A celery task completed without error
        WHEN:
            - API call is made to get tasks
        THEN:
            - The returned data includes the task result
        """
        task = PaperlessTask.objects.create(
            task_id=str(uuid.uuid4()),
            task_file_name="task_one.pdf",
            status=celery.states.SUCCESS,
            result="Success. New document id 1 created",
        )

        response = self.client.get(self.ENDPOINT)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)

        returned_data = response.data[0]

        self.assertEqual(returned_data["result"], "Success. New document id 1 created")
        self.assertEqual(returned_data["related_document"], "1")

    def test_task_result_with_error(self):
        """
        GIVEN:
            - A celery task completed with an exception
        WHEN:
            - API call is made to get tasks
        THEN:
            - The returned result is the exception info
        """
        task = PaperlessTask.objects.create(
            task_id=str(uuid.uuid4()),
            task_file_name="task_one.pdf",
            status=celery.states.FAILURE,
            result="test.pdf: Not consuming test.pdf: It is a duplicate.",
        )

        response = self.client.get(self.ENDPOINT)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)

        returned_data = response.data[0]

        self.assertEqual(
            returned_data["result"],
            "test.pdf: Not consuming test.pdf: It is a duplicate.",
        )

    def test_task_name_webui(self):
        """
        GIVEN:
            - Attempted celery task
            - Task was created through the webui
        WHEN:
            - API call is made to get tasks
        THEN:
            - Returned data include the filename
        """
        task = PaperlessTask.objects.create(
            task_id=str(uuid.uuid4()),
            task_file_name="test.pdf",
            task_name="documents.tasks.some_task",
            status=celery.states.SUCCESS,
        )

        response = self.client.get(self.ENDPOINT)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)

        returned_data = response.data[0]

        self.assertEqual(returned_data["task_file_name"], "test.pdf")

    def test_task_name_consume_folder(self):
        """
        GIVEN:
            - Attempted celery task
            - Task was created through the consume folder
        WHEN:
            - API call is made to get tasks
        THEN:
            - Returned data include the filename
        """
        task = PaperlessTask.objects.create(
            task_id=str(uuid.uuid4()),
            task_file_name="anothertest.pdf",
            task_name="documents.tasks.some_task",
            status=celery.states.SUCCESS,
        )

        response = self.client.get(self.ENDPOINT)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)

        returned_data = response.data[0]

        self.assertEqual(returned_data["task_file_name"], "anothertest.pdf")


class TestApiUser(DirectoriesMixin, APITestCase):
    ENDPOINT = "/api/users/"

    def setUp(self):
        super().setUp()

        self.user = User.objects.create_superuser(username="temp_admin")
        self.client.force_authenticate(user=self.user)

    def test_get_users(self):
        """
        GIVEN:
            - Configured users
        WHEN:
            - API call is made to get users
        THEN:
            - Configured users are provided
        """

        user1 = User.objects.create(
            username="testuser",
            password="test",
            first_name="Test",
            last_name="User",
        )

        response = self.client.get(self.ENDPOINT)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["count"], 2)
        returned_user2 = response.data["results"][1]

        self.assertEqual(returned_user2["username"], user1.username)
        self.assertEqual(returned_user2["password"], "**********")
        self.assertEqual(returned_user2["first_name"], user1.first_name)
        self.assertEqual(returned_user2["last_name"], user1.last_name)

    def test_create_user(self):
        """
        WHEN:
            - API request is made to add a user account
        THEN:
            - A new user account is created
        """

        user1 = {
            "username": "testuser",
            "password": "test",
            "first_name": "Test",
            "last_name": "User",
        }

        response = self.client.post(
            self.ENDPOINT,
            data=user1,
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        returned_user1 = User.objects.get(username="testuser")

        self.assertEqual(returned_user1.username, user1["username"])
        self.assertEqual(returned_user1.first_name, user1["first_name"])
        self.assertEqual(returned_user1.last_name, user1["last_name"])

    def test_delete_user(self):
        """
        GIVEN:
            - Existing user account
        WHEN:
            - API request is made to delete a user account
        THEN:
            - Account is deleted
        """

        user1 = User.objects.create(
            username="testuser",
            password="test",
            first_name="Test",
            last_name="User",
        )

        nUsers = User.objects.count()

        response = self.client.delete(
            f"{self.ENDPOINT}{user1.pk}/",
        )

        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)

        self.assertEqual(User.objects.count(), nUsers - 1)

    def test_update_user(self):
        """
        GIVEN:
            - Existing user accounts
        WHEN:
            - API request is made to update user account
        THEN:
            - The user account is updated, password only updated if not '****'
        """

        user1 = User.objects.create(
            username="testuser",
            password="test",
            first_name="Test",
            last_name="User",
        )

        initial_password = user1.password

        response = self.client.patch(
            f"{self.ENDPOINT}{user1.pk}/",
            data={
                "first_name": "Updated Name 1",
                "password": "******",
            },
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)

        returned_user1 = User.objects.get(pk=user1.pk)
        self.assertEqual(returned_user1.first_name, "Updated Name 1")
        self.assertEqual(returned_user1.password, initial_password)

        response = self.client.patch(
            f"{self.ENDPOINT}{user1.pk}/",
            data={
                "first_name": "Updated Name 2",
                "password": "123xyz",
            },
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)

        returned_user2 = User.objects.get(pk=user1.pk)
        self.assertEqual(returned_user2.first_name, "Updated Name 2")
        self.assertNotEqual(returned_user2.password, initial_password)


class TestApiGroup(DirectoriesMixin, APITestCase):
    ENDPOINT = "/api/groups/"

    def setUp(self):
        super().setUp()

        self.user = User.objects.create_superuser(username="temp_admin")
        self.client.force_authenticate(user=self.user)

    def test_get_groups(self):
        """
        GIVEN:
            - Configured groups
        WHEN:
            - API call is made to get groups
        THEN:
            - Configured groups are provided
        """

        group1 = Group.objects.create(
            name="Test Group",
        )

        response = self.client.get(self.ENDPOINT)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["count"], 1)
        returned_group1 = response.data["results"][0]

        self.assertEqual(returned_group1["name"], group1.name)

    def test_create_group(self):
        """
        WHEN:
            - API request is made to add a group
        THEN:
            - A new group is created
        """

        group1 = {
            "name": "Test Group",
        }

        response = self.client.post(
            self.ENDPOINT,
            data=group1,
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        returned_group1 = Group.objects.get(name="Test Group")

        self.assertEqual(returned_group1.name, group1["name"])

    def test_delete_group(self):
        """
        GIVEN:
            - Existing group
        WHEN:
            - API request is made to delete a group
        THEN:
            - Group is deleted
        """

        group1 = Group.objects.create(
            name="Test Group",
        )

        response = self.client.delete(
            f"{self.ENDPOINT}{group1.pk}/",
        )

        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)

        self.assertEqual(len(Group.objects.all()), 0)

    def test_update_group(self):
        """
        GIVEN:
            - Existing groups
        WHEN:
            - API request is made to update group
        THEN:
            - The group is updated
        """

        group1 = Group.objects.create(
            name="Test Group",
        )

        response = self.client.patch(
            f"{self.ENDPOINT}{group1.pk}/",
            data={
                "name": "Updated Name 1",
            },
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)

        returned_group1 = Group.objects.get(pk=group1.pk)
        self.assertEqual(returned_group1.name, "Updated Name 1")
