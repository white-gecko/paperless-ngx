import filecmp
import hashlib
import os
import shutil
import tempfile
from pathlib import Path
from unittest import mock

from django.core.management import call_command
from django.test import override_settings
from django.test import TestCase
from documents.file_handling import generate_filename
from documents.models import Document
from documents.tasks import update_document_archive_file
from documents.tests.utils import DirectoriesMixin


sample_file = os.path.join(os.path.dirname(__file__), "samples", "simple.pdf")


@override_settings(FILENAME_FORMAT="{correspondent}/{title}")
class TestArchiver(DirectoriesMixin, TestCase):
    def make_models(self):
        return Document.objects.create(
            checksum="A",
            title="A",
            content="first document",
            mime_type="application/pdf",
        )

    def test_archiver(self):

        doc = self.make_models()
        shutil.copy(
            sample_file,
            os.path.join(self.dirs.originals_dir, f"{doc.id:07}.pdf"),
        )

        call_command("document_archiver")

    def test_handle_document(self):

        doc = self.make_models()
        shutil.copy(
            sample_file,
            os.path.join(self.dirs.originals_dir, f"{doc.id:07}.pdf"),
        )

        update_document_archive_file(doc.pk)

        doc = Document.objects.get(id=doc.id)

        self.assertIsNotNone(doc.checksum)
        self.assertIsNotNone(doc.archive_checksum)
        self.assertTrue(os.path.isfile(doc.archive_path))
        self.assertTrue(os.path.isfile(doc.source_path))
        self.assertTrue(filecmp.cmp(sample_file, doc.source_path))
        self.assertEqual(doc.archive_filename, "none/A.pdf")

    def test_unknown_mime_type(self):
        doc = self.make_models()
        doc.mime_type = "sdgfh"
        doc.save()
        shutil.copy(sample_file, doc.source_path)

        update_document_archive_file(doc.pk)

        doc = Document.objects.get(id=doc.id)

        self.assertIsNotNone(doc.checksum)
        self.assertIsNone(doc.archive_checksum)
        self.assertIsNone(doc.archive_filename)
        self.assertTrue(os.path.isfile(doc.source_path))

    @override_settings(FILENAME_FORMAT="{title}")
    def test_naming_priorities(self):
        doc1 = Document.objects.create(
            checksum="A",
            title="document",
            content="first document",
            mime_type="application/pdf",
            filename="document.pdf",
        )
        doc2 = Document.objects.create(
            checksum="B",
            title="document",
            content="second document",
            mime_type="application/pdf",
            filename="document_01.pdf",
        )
        shutil.copy(sample_file, os.path.join(self.dirs.originals_dir, "document.pdf"))
        shutil.copy(
            sample_file,
            os.path.join(self.dirs.originals_dir, "document_01.pdf"),
        )

        update_document_archive_file(doc2.pk)
        update_document_archive_file(doc1.pk)

        doc1 = Document.objects.get(id=doc1.id)
        doc2 = Document.objects.get(id=doc2.id)

        self.assertEqual(doc1.archive_filename, "document.pdf")
        self.assertEqual(doc2.archive_filename, "document_01.pdf")


class TestMakeIndex(TestCase):
    @mock.patch("documents.management.commands.document_index.index_reindex")
    def test_reindex(self, m):
        call_command("document_index", "reindex")
        m.assert_called_once()

    @mock.patch("documents.management.commands.document_index.index_optimize")
    def test_optimize(self, m):
        call_command("document_index", "optimize")
        m.assert_called_once()


class TestRenamer(DirectoriesMixin, TestCase):
    @override_settings(FILENAME_FORMAT="")
    def test_rename(self):
        doc = Document.objects.create(title="test", mime_type="image/jpeg")
        doc.filename = generate_filename(doc)
        doc.archive_filename = generate_filename(doc, archive_filename=True)
        doc.save()

        Path(doc.source_path).touch()
        Path(doc.archive_path).touch()

        with override_settings(FILENAME_FORMAT="{correspondent}/{title}"):
            call_command("document_renamer")

        doc2 = Document.objects.get(id=doc.id)

        self.assertEqual(doc2.filename, "none/test.jpg")
        self.assertEqual(doc2.archive_filename, "none/test.pdf")
        self.assertFalse(os.path.isfile(doc.source_path))
        self.assertFalse(os.path.isfile(doc.archive_path))
        self.assertTrue(os.path.isfile(doc2.source_path))
        self.assertTrue(os.path.isfile(doc2.archive_path))


class TestCreateClassifier(TestCase):
    @mock.patch(
        "documents.management.commands.document_create_classifier.train_classifier",
    )
    def test_create_classifier(self, m):
        call_command("document_create_classifier")

        m.assert_called_once()


class TestSanityChecker(DirectoriesMixin, TestCase):
    def test_no_issues(self):
        with self.assertLogs() as capture:
            call_command("document_sanity_checker")

        self.assertEqual(len(capture.output), 1)
        self.assertIn("Sanity checker detected no issues.", capture.output[0])

    def test_errors(self):
        doc = Document.objects.create(
            title="test",
            content="test",
            filename="test.pdf",
            checksum="abc",
        )
        Path(doc.source_path).touch()
        Path(doc.thumbnail_path).touch()

        with self.assertLogs() as capture:
            call_command("document_sanity_checker")

        self.assertEqual(len(capture.output), 2)
        self.assertIn("Checksum mismatch. Stored: abc, actual:", capture.output[1])
