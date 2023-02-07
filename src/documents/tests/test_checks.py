from unittest import mock

from django.core.checks import Error
from django.test import TestCase
from documents.checks import parser_check


class TestDocumentChecks(TestCase):
    def test_parser_check(self):

        self.assertEqual(parser_check(None), [])

        with mock.patch("documents.checks.document_consumer_declaration.send") as m:
            m.return_value = []

            self.assertEqual(
                parser_check(None),
                [
                    Error(
                        "No parsers found. This is a bug. The consumer won't be "
                        "able to consume any documents without parsers.",
                    ),
                ],
            )
